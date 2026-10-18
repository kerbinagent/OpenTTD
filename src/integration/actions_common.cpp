/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file actions_common.cpp Shared validated parameter and snapshot helpers. */

#include "../stdafx.h"
#include "actions_internal.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../map_func.h"
#include "../rail.h"
#include "../road.h"
#include "../station_base.h"
#include "../timer/timer_game_tick.h"
#include "../vehicle_base.h"
#include "../vehicle_func.h"

#include "../safeguards.h"

namespace Integration::Detail {

int64_t Parameters::Int(const char *key, int64_t minimum, int64_t maximum, std::optional<int64_t> fallback) const
{
	auto it = this->data.find(key);
	if (it == this->data.end()) {
		if (fallback.has_value()) return *fallback;
		throw InvalidParameters(std::string("Missing parameter: ") + key);
	}
	if (!it->is_number_integer() || (it->is_number_unsigned() && it->get<uint64_t>() > static_cast<uint64_t>(INT64_MAX))) throw InvalidParameters(std::string("Expected integer: ") + key);
	int64_t value = it->get<int64_t>();
	if (value < minimum || value > maximum) throw InvalidParameters(std::string("Out of range: ") + key);
	return value;
}

bool Parameters::Bool(const char *key, bool fallback) const
{
	auto it = this->data.find(key);
	if (it == this->data.end()) return fallback;
	if (!it->is_boolean()) throw InvalidParameters(std::string("Expected boolean: ") + key);
	return it->get<bool>();
}

std::string Parameters::Text(const char *key, const std::string &fallback, size_t maximum) const
{
	auto it = this->data.find(key);
	if (it == this->data.end()) return fallback;
	if (!it->is_string()) throw InvalidParameters(std::string("Expected string: ") + key);
	auto value = it->get<std::string>();
	if (value.size() > maximum || value.find('\0') != std::string::npos) throw InvalidParameters(std::string("Invalid string: ") + key);
	return value;
}

TileIndex Parameters::Tile(const char *key) const
{
	auto it = this->data.find(key);
	if (it == this->data.end()) throw InvalidParameters(std::string("Missing tile: ") + key);
	TileIndex tile;
	if (it->is_object()) {
		if (it->size() != 2 || !it->contains("x") || !it->contains("y")) throw InvalidParameters("Tile coordinates require only x and y");
		Parameters coordinates(*it);
		tile = TileXY(coordinates.Int("x", 0, Map::SizeX() - 1), coordinates.Int("y", 0, Map::SizeY() - 1));
	} else {
		tile = TileIndex(static_cast<uint32_t>(this->Int(key, 0, Map::Size() - 1)));
	}
	if (!IsValidTile(tile)) throw InvalidParameters(std::string("Invalid tile: ") + key);
	return tile;
}

StationID Parameters::Station(const char *key, bool optional) const
{
	/* Select the ordinary explicit-new option; gray-sign reuse needs a handle. */
	if (optional && !this->data.contains(key)) return NEW_STATION;
	const auto &handle = this->data.at(key);
	if (handle.at("engine_instance_id").get_ref<const std::string &>() != InstanceId()) throw InvalidParameters("Station target belongs to another engine instance");
	auto id = StationID(handle.at("id").get<uint16_t>());
	const BaseStation *station = BaseStation::GetIfValid(id);
	if (station == nullptr || station->owner != _current_company) throw InvalidParameters("Station target is invalid or not owned");
	if (station->GetInstanceGeneration() != handle.at("generation").get<uint64_t>()) throw InvalidParameters("Station target is stale");
	return id;
}

VehicleID Parameters::Vehicle(const char *key) const
{
	const auto &handle = this->data.at(key);
	if (handle.at("engine_instance_id").get_ref<const std::string &>() != InstanceId()) throw InvalidParameters("Vehicle target belongs to another engine instance");
	auto id = VehicleID(handle.at("id").get<uint32_t>());
	const ::Vehicle *vehicle = ::Vehicle::GetIfValid(id);
	if (vehicle == nullptr || vehicle->owner != _current_company || !IsCompanyBuildableVehicleType(vehicle)) throw InvalidParameters("Vehicle target is invalid or not owned");
	if (vehicle->GetInstanceGeneration() != handle.at("generation").get<uint64_t>()) throw InvalidParameters("Vehicle target is stale");
	return id;
}

RoadType Parameters::Road() const { return static_cast<RoadType>(this->Int("road_type", 0, ROADTYPE_END - 1, ROADTYPE_ROAD)); }
RailType Parameters::Rail() const { return static_cast<RailType>(this->Int("rail_type", 0, RAILTYPE_END - 1, RAILTYPE_RAIL)); }
Axis Parameters::GetAxis() const
{
	auto value = this->Text("axis");
	if (value == "x") return Axis::X;
	if (value == "y") return Axis::Y;
	throw InvalidParameters("axis must be x or y");
}
DiagDirection Parameters::Direction() const { return static_cast<DiagDirection>(this->Int("direction", 0, 3)); }
Track Parameters::GetTrack() const
{
	static const std::array<std::string_view, 6> names = {"x", "y", "upper", "lower", "left", "right"};
	auto value = this->Text("track");
	auto it = std::ranges::find(names, value);
	if (it == names.end()) throw InvalidParameters("Invalid track; use x, y, upper, lower, left or right");
	return static_cast<Track>(it - names.begin());
}
RoadStopType Parameters::StopType() const
{
	auto value = this->Text("stop_type");
	if (value == "bus") return RoadStopType::Bus;
	if (value == "truck") return RoadStopType::Truck;
	throw InvalidParameters("stop_type must be bus or truck");
}
TransportType Parameters::Transport(bool water) const
{
	auto value = this->Text("transport");
	if (value == "rail") return TransportType::Rail;
	if (value == "road") return TransportType::Road;
	if (water && value == "water") return TransportType::Water;
	throw InvalidParameters(water ? "transport must be rail, road or water" : "transport must be rail or road");
}

void BoundedArea(TileIndex start, TileIndex end, uint max_area)
{
	uint width = std::abs(static_cast<int>(TileX(start)) - static_cast<int>(TileX(end))) + 1;
	uint height = std::abs(static_cast<int>(TileY(start)) - static_cast<int>(TileY(end))) + 1;
	if (width * height > max_area || width > MAX_MAP_SIZE || height > MAX_MAP_SIZE) throw InvalidParameters("Construction area exceeds the per-action budget");
}

void RequireCompany()
{
	if (!Company::IsValidID(_current_company)) throw InvalidParameters("A valid company context is required");
}

ActionResult Submitted(bool accepted)
{
	if (accepted) return {true, {}, {}};
	if (auto failure = TakeSubmittedPostFailure()) return {false, {}, "gameplay_rejected", std::move(failure)};
	return {false, "The engine rejected the command before queuing", "command_rejected"};
}

Json TileCoordinates(TileIndex tile)
{
	return Json{{"tile", tile.base()}, {"x", TileX(tile)}, {"y", TileY(tile)}};
}

const char *VehicleTypeName(VehicleType type)
{
	switch (type) {
		case VehicleType::Train: return "rail";
		case VehicleType::Road: return "road";
		case VehicleType::Ship: return "ship";
		case VehicleType::Aircraft: return "aircraft";
		default: return "invalid";
	}
}

Json SnapshotBase()
{
	return {{"tick", TimerGameTick::counter}, {"company_id", _current_company.base()}};
}

} // namespace Integration::Detail
