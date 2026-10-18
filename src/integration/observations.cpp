/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file observations.cpp Bounded, read-only normal-company planning reports. */

#include "../stdafx.h"
#include "actions_internal.h"
#include "actions_limits.h"
#include "../bridge.h"
#include "../command_func.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../cargotype.h"
#include "../core/string_builder.hpp"
#include "../core/utf8.hpp"
#include "../economy_func.h"
#include "../house.h"
#include "../landscape.h"
#include "../landscape_cmd.h"
#include "../linkgraph/linkgraph_type.h"
#include "../map_func.h"
#include "../newgrf_airport.h"
#include "../newgrf_object.h"
#include "../newgrf_industries.h"
#include "../newgrf_roadstop.h"
#include "../newgrf_station.h"
#include "../object_map.h"
#include "../settings_type.h"
#include "../slope_func.h"
#include "../station_cmd.h"
#include "../station_func.h"
#include "../station_map.h"
#include "../string_func.h"
#include "../strings_func.h"
#include "../tile_cmd.h"
#include "../tile_map.h"
#include "../tilearea_airportlayout.h"
#include "../timer/timer_game_calendar.h"
#include "../timer/timer_game_economy.h"
#include "../timer/timer_game_tick.h"
#include "../town.h"
#include "../town_map.h"

#include <array>
#include <algorithm>
#include <limits>

#include "table/strings.h"

#include "../safeguards.h"

namespace Integration::Detail {
namespace {

constexpr size_t REPORT_BYTES = 40 * 1024;
constexpr size_t REPORT_TEXT_BYTES = 1024;
constexpr size_t PAYMENT_RATES_BYTES = 8 * 1024;
constexpr size_t BRIDGES_BYTES = 24 * 1024;
constexpr size_t BRIDGE_MATERIAL_BYTES = 512;

template <Commands C>
constexpr DoCommandFlags PreviewFlags()
{
	/* Match the flags used by the normal command test, without executing it. */
	return CommandFlagsToDCFlags(GetCommandFlags<C>()) | DoCommandFlag::QueryCost;
}

Json Bounded(Json result, size_t maximum = REPORT_BYTES)
{
	if (result.dump().size() > maximum) throw InvalidParameters("Report exceeds the observation byte budget");
	return result;
}

std::string Text(std::string value)
{
	if (value.size() > REPORT_TEXT_BYTES) throw InvalidParameters("Report text exceeds the observation byte budget");
	return value;
}

Json CalendarDate(TimerGameCalendar::Date date)
{
	auto value = TimerGameCalendar::ConvertDateToYMD(date);
	return {{"year", value.year.base()}, {"month", value.month + 1}, {"day", value.day}};
}

const char *DistributionName(DistributionType type)
{
	switch (type) {
		case DistributionType::Manual: return "manual";
		case DistributionType::Asymmetric: return "asymmetric";
		case DistributionType::Symmetric: return "symmetric";
		default: return "manual";
	}
}

Json WorldReport()
{
	Json result = SnapshotBase();
	const char *climate = "temperate";
	switch (_settings_game.game_creation.landscape) {
		case LandscapeType::Temperate: break;
		case LandscapeType::Arctic: climate = "arctic"; break;
		case LandscapeType::Tropic: climate = "tropical"; break;
		case LandscapeType::Toyland: climate = "toyland"; break;
	}
	result["map"] = {{"width", Map::SizeX()}, {"height", Map::SizeY()}, {"climate", climate}};
	auto calendar = CalendarDate(TimerGameCalendar::date);
	calendar["date_day"] = TimerGameCalendar::date.base();
	calendar["day_fraction"] = TimerGameCalendar::date_fract;
	result["calendar"] = std::move(calendar);
	auto economy = TimerGameEconomy::ConvertDateToYMD(TimerGameEconomy::date);
	result["economy"] = {{"date_day", TimerGameEconomy::date.base()}, {"year", economy.year.base()},
		{"month", economy.month + 1}, {"day", economy.day}, {"day_fraction", TimerGameEconomy::date_fract}};
	bool wallclock = TimerGameEconomy::UsingWallclockUnits();
	result["timekeeping"] = {{"mode", wallclock ? "wallclock" : "calendar"},
		{"minutes_per_calendar_year", _settings_game.economy.minutes_per_calendar_year},
		{"ticks_per_day", Ticks::DAY_TICKS}, {"financial_period_unit", wallclock ? "minute" : "month"}};
	const auto &v = _settings_game.vehicle;
	const auto &l = _settings_game.linkgraph;
	result["rules"] = {
		{"station_spread", _settings_game.station.station_spread},
		{"modified_catchment", _settings_game.station.modified_catchment},
		{"serve_neutral_industries", _settings_game.station.serve_neutral_industries},
		{"distant_join_stations", _settings_game.station.distant_join_stations},
		{"station_noise_level", _settings_game.economy.station_noise_level},
		{"infrastructure_maintenance", _settings_game.economy.infrastructure_maintenance},
		{"local_authority_distance", _settings_game.economy.dist_local_authority},
		{"max_train_length", v.max_train_length},
		{"vehicle_limits", {{"train", v.max_trains}, {"road", v.max_roadveh},
			{"aircraft", v.max_aircraft}, {"ship", v.max_ships}}},
		{"cargo_distribution", {{"passengers", DistributionName(l.distribution_pax)},
			{"mail", DistributionName(l.distribution_mail)}, {"armoured", DistributionName(l.distribution_armoured)},
			{"default", DistributionName(l.distribution_default)}}},
	};
	return Bounded(std::move(result));
}

Json CargoPaymentRates(const Json &parameters)
{
	if (!parameters.is_object() || parameters.size() != 1 || !parameters.contains("cargo_id")) throw InvalidParameters("Expected only cargo_id");
	Parameters p(parameters);
	const CargoType cargo = static_cast<CargoType>(p.Int("cargo_id", 0, NUM_CARGO - 1));
	if (!CargoSpec::Get(cargo)->IsValid() || !_standard_cargo_mask.Test(cargo)) throw InvalidParameters("Cargo is unavailable");
	const bool wallclock = TimerGameEconomy::UsingWallclockUnits();
	const uint display_step = wallclock ? CargoPaymentRatesGraph::DISPLAY_SECOND_STEP : CargoPaymentRatesGraph::DISPLAY_DAY_STEP;
	Json result = SnapshotBase();
	result["cargo_id"] = to_underlying(cargo);
	result["amount"] = CargoPaymentRatesGraph::AMOUNT;
	result["distance_tiles"] = CargoPaymentRatesGraph::DISTANCE_TILES;
	result["money_unit"] = "native_money";
	result["time_unit"] = wallclock ? "wallclock_seconds" : "economy_days";
	result["income_scope"] = "base_delivery";
	result["samples"] = Json::array();
	for (uint i = 1; i <= CargoPaymentRatesGraph::SAMPLE_COUNT; ++i) {
		const uint16_t periods = i * CargoPaymentRatesGraph::TRANSIT_PERIOD_STEP;
		result["samples"].push_back({{"transit_periods", periods}, {"display_time", i * display_step},
			{"income", GetTransportedGoodsIncome(CargoPaymentRatesGraph::AMOUNT, CargoPaymentRatesGraph::DISTANCE_TILES, periods, cargo).base()}});
	}
	return Bounded(std::move(result), PAYMENT_RATES_BYTES);
}

/** Match other display previews: plain UTF-8, truncated only between codepoints. */
void PutBridgeMaterial(Json &item, std::string value)
{
	value = StrMakeValid(std::move(value), StringValidationSetting::AllowNewline);
	std::string plain;
	StringBuilder builder(plain);
	for (char32_t c : Utf8View(value)) if (!IsTextDirectionChar(c)) builder.PutUtf8(c);
	size_t length = std::min(plain.size(), BRIDGE_MATERIAL_BYTES);
	while (length > 0 && length < plain.size() && IsUtf8Part(plain[length])) --length;
	item["material"] = plain.substr(0, length);
	item["material_truncated"] = length < plain.size();
}

Json BridgeCatalog(const Json &parameters)
{
	if (!parameters.is_object() || !parameters.empty()) throw InvalidParameters("Expected empty bridge catalog parameters");
	Json result = SnapshotBase();
	result["transports"] = {"rail", "road"};
	result["span_unit"] = "interior_tiles_excluding_heads";
	result["speed_unit"] = "kmh_ish";
	result["items"] = Json::array();
	for (BridgeType type = 0; type < MAX_BRIDGES; ++type) {
		const BridgeSpec *spec = GetBridgeSpec(type);
		if (!CheckBridgeAvailability(type, spec->min_length, {}).Succeeded()) continue;
		Json item = {{"bridge_type", type}, {"minimum_span_tiles", spec->min_length},
			{"maximum_span_tiles", std::min(spec->max_length, _settings_game.construction.max_bridge_length)},
			{"maximum_speed", spec->speed == UINT16_MAX ? Json(nullptr) : Json(spec->speed)}};
		PutBridgeMaterial(item, GetString(spec->material));
		result["items"].push_back(std::move(item));
	}
	return Bounded(std::move(result), BRIDGES_BYTES);
}

Json OwnerReport(TileIndex tile, Owner owner, StringID type, uint slot)
{
	Json item = {{"slot", slot}, {"kind", "other"}, {"company_id", nullptr},
		{"town_id", nullptr}, {"label", ""}};
	if (owner == OWNER_NONE) item["kind"] = "none";
	else if (owner == OWNER_WATER) item["kind"] = "water";
	else if (owner == OWNER_TOWN) {
		item["kind"] = "town";
		const Town *town = ClosestTownFromTile(tile, UINT_MAX);
		if (town != nullptr) item["town_id"] = Handle(town);
	} else if (Company::IsValidID(owner)) {
		item["kind"] = "company";
		item["company_id"] = owner.base();
	}
	if (owner == OWNER_NONE || owner == OWNER_WATER) {
		item["label"] = Text(GetString(type, STR_LAND_AREA_INFORMATION_OWNER_N_A, std::monostate{}));
	} else {
		auto args = GetParamsForOwnedBy(owner, tile);
		item["label"] = Text(GetStringWithArgs(type, args));
	}
	return item;
}

Json ErrorPreview(const CommandCost &cost)
{
	if (cost.Succeeded()) return {{"status", "available"}, {"cost", cost.GetCost().base()},
		{"error", nullptr}, {"advisory_tick", TimerGameTick::counter}};
	CommandCost copy = cost;
	std::string message = !copy.GetEncodedMessage().empty() ? copy.GetEncodedMessage().GetDecodedString() :
		cost.GetErrorMessage() == INVALID_STRING_ID ? "Native query test failed" : GetString(cost.GetErrorMessage());
	return {{"status", "blocked"}, {"cost", nullptr},
		{"error", {{"code", "query_test_failed"}, {"message", Text(std::move(message))}}},
		{"advisory_tick", TimerGameTick::counter}};
}

Json LandInfo(const Json &parameters)
{
	Parameters p(parameters);
	TileIndex tile = p.Tile("tile");
	TileDesc desc{};
	desc.owner_type[0] = STR_LAND_AREA_INFORMATION_OWNER;
	GetTileDesc(tile, desc);
	Json result = SnapshotBase();
	result["tile"] = TileCoordinates(tile);
	result["tile_type"] = to_underlying(GetTileType(tile));
	result["height"] = TileHeight(tile);
	result["slope"] = to_underlying(GetTileSlope(tile));
	result["description"] = Text(GetString(desc.str, desc.dparam));
	result["owners"] = Json::array();
	for (uint slot = 0; slot < 4; ++slot) {
		if (desc.owner_type[slot] != STR_NULL) result["owners"].push_back(OwnerReport(tile, desc.owner[slot], desc.owner_type[slot], slot));
	}
	const Town *authority = ClosestTownFromTile(tile, _settings_game.economy.dist_local_authority);
	result["local_authority"] = authority == nullptr ? Json(nullptr) : Handle(authority);
	result["build_date"] = desc.build_date == CalendarTime::INVALID_DATE ? Json(nullptr) : CalendarDate(desc.build_date);
	result["house"] = nullptr;
	result["object"] = nullptr;
	result["station_spec"] = nullptr;
	if (IsTileType(tile, TileType::House)) {
		auto type = GetHouseType(tile);
		const HouseSpec *house = HouseSpec::Get(type);
		result["house"] = {{"house_type", type}, {"name", Text(GetString(house->building_name))}};
	} else if (IsTileType(tile, TileType::Object)) {
		auto type = GetObjectType(tile);
		const ObjectSpec *object = ObjectSpec::Get(type);
		if (object != nullptr) result["object"] = {{"object_type", type}, {"name", Text(GetString(object->name))}};
	} else if (IsRailStationTile(tile)) {
		const StationSpec *spec = GetStationSpec(tile);
		if (spec != nullptr) result["station_spec"] = {{"class_id", spec->class_index.base()},
			{"spec_index", spec->index}, {"name", Text(GetString(spec->name))}};
	}
	CargoArray acceptance{};
	CargoTypes always_accepted{};
	AddAcceptedCargo(tile, acceptance, always_accepted);
	result["accepted_cargo"] = Json::array();
	for (CargoType cargo : EnumRange(NUM_CARGO)) {
		if (acceptance[cargo] == 0 && !always_accepted.Test(cargo)) continue;
		const CargoSpec *spec = CargoSpec::Get(cargo);
		if (!spec->IsValid()) continue;
		result["accepted_cargo"].push_back({{"cargo_id", to_underlying(cargo)},
			{"acceptance_eighths", acceptance[cargo]}, {"always_accepted", always_accepted.Test(cargo)}});
	}
	result["clear_preview"] = ErrorPreview(Command<Commands::LandscapeClear>::Do(PreviewFlags<Commands::LandscapeClear>(), tile));
	return Bounded(std::move(result));
}

const char *RotationName(Direction direction)
{
	switch (direction) {
		case Direction::N: return "north";
		case Direction::E: return "east";
		case Direction::S: return "south";
		case Direction::W: return "west";
		default: return "north";
	}
}

struct Site {
	TileIndex anchor{};
	uint x = 0;
	uint y = 0;
	uint width = 1;
	uint height = 1;
	uint radius = 0;
	const char *kind = "rail";
	Json spec_class = nullptr;
	Json spec_index = nullptr;
	Json airport_type = nullptr;
	Json layout = nullptr;
	Json rotation = nullptr;
	bool passengers_only = false;
	bool freight_only = false;
	const AirportSpec *airport = nullptr;
	Direction airport_rotation = Direction::N;
	StationID station = NEW_STATION;
	bool adjacent = true;
};

Site ResolveSite(const Json &parameters)
{
	if (!parameters.is_object() || !parameters.contains("build_action") || !parameters.contains("build_parameters") ||
			!parameters.at("build_action").is_string() || !parameters.at("build_parameters").is_object()) throw InvalidParameters("Invalid station site selection");
	std::string action = parameters.at("build_action").get<std::string>();
	const Json &args = parameters.at("build_parameters");
	Parameters p(args);
	Site site;
	site.anchor = p.Tile("tile");
	site.x = TileX(site.anchor);
	site.y = TileY(site.anchor);
	site.station = p.Station("station_id", true);
	site.adjacent = p.Bool("adjacent", true);
	if (action == "rail.station.build") {
		const auto selected = ResolveRailStationSpecSelection(p, false);
		Axis axis = p.GetAxis();
		site.width = static_cast<uint>(p.Int("tracks", 1, NATIVE_MAX_STATION_EXTENT));
		site.height = static_cast<uint>(p.Int("length", 1, NATIVE_MAX_STATION_EXTENT));
		if (axis == Axis::X) std::swap(site.width, site.height);
		site.radius = _settings_game.station.modified_catchment ? CA_TRAIN : CA_UNMODIFIED;
		site.spec_class = selected.spec_class;
		site.spec_index = selected.spec_index;
	} else if (action == "road.stop.build") {
		RoadStopType stop = p.StopType();
		const auto selected = ResolveRoadStopSpecSelection(p, stop == RoadStopType::Bus ? StationType::Bus : StationType::Truck);
		site.kind = stop == RoadStopType::Bus ? "bus" : "truck";
		site.passengers_only = stop == RoadStopType::Bus;
		site.freight_only = stop == RoadStopType::Truck;
		site.width = static_cast<uint>(p.Int("width", 1, NATIVE_MAX_STATION_EXTENT, 1));
		site.height = static_cast<uint>(p.Int("length", 1, NATIVE_MAX_STATION_EXTENT, 1));
		site.radius = _settings_game.station.modified_catchment ?
			(stop == RoadStopType::Bus ? CA_BUS : CA_TRUCK) : CA_UNMODIFIED;
		site.spec_class = selected.spec_class;
		site.spec_index = selected.spec_index;
	} else if (action == "water.dock.build") {
		site.kind = "dock";
		site.radius = _settings_game.station.modified_catchment ? CA_DOCK : CA_UNMODIFIED;
		DiagDirection direction = GetInclinedSlopeDirection(GetTileSlope(site.anchor));
		if (direction == DiagDirection::Invalid) throw InvalidParameters("Dock site has no valid orientation");
		direction = ReverseDiagDir(direction);
		int next_x = static_cast<int>(site.x);
		int next_y = static_cast<int>(site.y);
		switch (direction) {
			case DiagDirection::NE: --next_x; break;
			case DiagDirection::SE: ++next_y; break;
			case DiagDirection::SW: ++next_x; break;
			case DiagDirection::NW: --next_y; break;
			default: break;
		}
		int far_x = next_x + (next_x - static_cast<int>(site.x));
		int far_y = next_y + (next_y - static_cast<int>(site.y));
		if (next_x < 0 || next_y < 0 || far_x < 0 || far_y < 0 ||
			next_x >= static_cast<int>(Map::SizeX()) || next_y >= static_cast<int>(Map::SizeY()) ||
			far_x >= static_cast<int>(Map::SizeX()) || far_y >= static_cast<int>(Map::SizeY()) ||
			!IsValidTile(TileXY(next_x, next_y)) || !IsValidTile(TileXY(far_x, far_y)))
			throw InvalidParameters("Dock site points outside the valid map");
		site.x = std::min<uint>(site.x, next_x);
		site.y = std::min<uint>(site.y, next_y);
		site.width = next_x == static_cast<int>(TileX(site.anchor)) ? 1 : 2;
		site.height = next_y == static_cast<int>(TileY(site.anchor)) ? 1 : 2;
	} else if (action == "airport.build") {
		auto type = static_cast<uint8_t>(p.Int("airport_type", 0, NUM_AIRPORTS - 1));
		const AirportSpec *airport = AirportSpec::Get(type);
		auto layout = static_cast<uint8_t>(p.Int("layout", 0, 255, 0));
		if (airport == nullptr || !airport->IsAvailable() || layout >= airport->layouts.size() ||
				!airport->IsWithinMapBounds(layout, site.anchor)) throw InvalidParameters("Airport selection is unavailable");
		site.kind = "airport";
		site.airport = airport;
		site.airport_type = type;
		site.layout = layout;
		site.airport_rotation = airport->layouts[layout].rotation;
		site.rotation = RotationName(site.airport_rotation);
		site.width = airport->size_x;
		site.height = airport->size_y;
		if (site.airport_rotation == Direction::E || site.airport_rotation == Direction::W) std::swap(site.width, site.height);
		site.radius = _settings_game.station.modified_catchment ? airport->catchment : CA_UNMODIFIED;
	} else {
		throw InvalidParameters("Unsupported station site action");
	}
	if (site.width == 0 || site.height == 0 || site.width > NATIVE_MAX_STATION_EXTENT ||
			site.height > NATIVE_MAX_STATION_EXTENT || site.radius > MAX_CATCHMENT ||
			site.x + site.width > Map::SizeX() || site.y + site.height > Map::SizeY())
		throw InvalidParameters("Station site exceeds the observation area budget");
	return site;
}

CommandCost TestSite(const Json &parameters, const Site &site)
{
	const std::string action = parameters.at("build_action").get<std::string>();
	Parameters p(parameters.at("build_parameters"));
	if (action == "rail.station.build") return Command<Commands::BuildRailStation>::Do(PreviewFlags<Commands::BuildRailStation>(),
		site.anchor, p.Rail(), p.GetAxis(), static_cast<uint8_t>(p.Int("tracks", 1, NATIVE_MAX_STATION_EXTENT)),
		static_cast<uint8_t>(p.Int("length", 1, NATIVE_MAX_STATION_EXTENT)), StationClassID(site.spec_class.get<uint16_t>()), site.spec_index.get<uint16_t>(), site.station, site.adjacent);
	if (action == "road.stop.build") return Command<Commands::BuildRoadStop>::Do(PreviewFlags<Commands::BuildRoadStop>(),
		site.anchor, static_cast<uint8_t>(site.width), static_cast<uint8_t>(site.height),
		p.StopType(), p.Bool("drive_through"), p.Direction(), p.Road(), RoadStopClassID(site.spec_class.get<uint16_t>()), site.spec_index.get<uint16_t>(), site.station, site.adjacent);
	if (action == "water.dock.build") return Command<Commands::BuildDock>::Do(PreviewFlags<Commands::BuildDock>(),
		site.anchor, site.station, site.adjacent);
	if (action == "airport.build") return Command<Commands::BuildAirport>::Do(PreviewFlags<Commands::BuildAirport>(),
		site.anchor, static_cast<uint8_t>(p.Int("airport_type", 0, NUM_AIRPORTS - 1)),
		static_cast<uint8_t>(p.Int("layout", 0, 255, 0)), site.station, site.adjacent);
	throw InvalidParameters("Unsupported station site action");
}

/** Plain bounded label; sanitization precedes clipping so JSON control escaping stays bounded. */
std::pair<std::string, bool> SiteText(std::string value, size_t maximum)
{
	value = StrMakeValid(std::move(value), {StringValidationSetting::ReplaceWithQuestionMark, StringValidationSetting::ReplaceTabCrNlWithSpace});
	std::string plain;
	StringBuilder builder(plain);
	for (char32_t c : Utf8View(value)) if (c >= 0x20 && !IsTextDirectionChar(c)) builder.PutUtf8(c);
	size_t length = std::min(plain.size(), maximum);
	while (length > 0 && length < plain.size() && IsUtf8Part(plain[length])) --length;
	return {plain.substr(0, length), length < plain.size()};
}

Json SiteFeasibility(const CommandCost &cost)
{
	if (cost.Succeeded()) return {{"status", "query_test_passed"}, {"cost", cost.GetCost().base()},
		{"error", nullptr}, {"advisory_tick", TimerGameTick::counter}, {"cash_checked", false},
		{"reserved", false}, {"build_result_guaranteed", false}};
	CommandCost copy = cost;
	std::string message = !copy.GetEncodedMessage().empty() ? copy.GetEncodedMessage().GetDecodedString() :
		cost.GetErrorMessage() == INVALID_STRING_ID ? "Native query test failed" : GetString(cost.GetErrorMessage());
	return {{"status", "query_test_failed"}, {"cost", nullptr},
		{"error", {{"code", "query_test_failed"}, {"message", SiteText(std::move(message), REPORT_TEXT_BYTES).first}}},
		{"advisory_tick", TimerGameTick::counter}, {"cash_checked", false},
		{"reserved", false}, {"build_result_guaranteed", false}};
}

Json StationSite(const Json &parameters)
{
	Site site = ResolveSite(parameters);
	AdmitWorldTextStart();
	CommandCost test = TestSite(parameters, site);
	Json result = SnapshotBase();
	result["build_action"] = parameters.at("build_action");
	result["selection"] = {{"kind", site.kind}, {"tile", TileCoordinates(site.anchor)},
		{"spec_class", site.spec_class}, {"spec_index", site.spec_index},
		{"airport_type", site.airport_type}, {"layout", site.layout}, {"rotation", site.rotation}};
	result["footprint"] = {{"x", site.x}, {"y", site.y}, {"width", site.width},
		{"height", site.height}, {"tile_count", site.width * site.height}};
	result["catchment"] = {{"radius", site.radius}, {"coverage_model", "gui_rectangle_advisory"},
		{"joined_coverage_excluded", true}, {"irregular_airport_tiles_modeled", false}};
	TileIndex north = TileXY(site.x, site.y);
	IndustryPresentationPsaOverlay callback_psa{nullptr, nullptr, GrfID{}};
	auto [acceptance, always] = GetAcceptanceAroundTiles(north, site.width, site.height, site.radius, &callback_psa);
	CargoArray production = GetProductionAroundTiles(north, site.width, site.height, site.radius, &callback_psa);
	result["accepted_cargo"] = Json::array();
	result["supplied_cargo"] = Json::array();
	for (CargoType cargo : EnumRange(NUM_CARGO)) {
		const CargoSpec *spec = CargoSpec::Get(cargo);
		if (!spec->IsValid()) continue;
		bool is_passenger = IsCargoInClass(cargo, CargoClass::Passengers);
		bool eligible = !((site.passengers_only && !is_passenger) || (site.freight_only && is_passenger));
		if (acceptance[cargo] == 0 && !always.Test(cargo) && production[cargo] == 0) continue;
		auto [name, name_truncated] = SiteText(GetString(spec->name), 64);
		if (acceptance[cargo] > 0 || always.Test(cargo)) result["accepted_cargo"].push_back({
			{"cargo_id", to_underlying(cargo)}, {"name", name}, {"name_truncated", name_truncated},
			{"acceptance_eighths", acceptance[cargo]}, {"preview_threshold_met", acceptance[cargo] >= 8},
			{"facility_eligible", eligible}, {"always_accepted", always.Test(cargo)}});
		if (production[cargo] > 0) result["supplied_cargo"].push_back({
			{"cargo_id", to_underlying(cargo)}, {"name", name}, {"name_truncated", name_truncated},
			{"source_count", production[cargo]}, {"preview_threshold_met", production[cargo] >= 1}});
	}
	CommandCost authority = CheckIfAuthorityAllowsNewStation(site.anchor, DoCommandFlag::QueryCost);
	const Town *town = ClosestTownFromTile(site.anchor, _settings_game.economy.dist_local_authority);
	Json noise = nullptr;
	if (site.airport != nullptr) {
		uint distance = 0;
		const auto &tiles = site.airport->layouts[site.layout.get<uint8_t>()].tiles;
		Town *nearest = AirportGetNearestTown(site.airport, site.airport_rotation, site.anchor,
			AirportTileTableIterator{tiles, site.anchor, false}, distance);
		if (nearest != nullptr) {
			town = nearest;
			noise = {{"enabled", _settings_game.economy.station_noise_level},
				{"current", nearest->noise_reached},
				{"increment", GetAirportNoiseLevelForDistance(site.airport, distance)},
				{"limit", nearest->MaxTownNoise()}};
		}
	}
	result["authority"] = {{"town_id", town == nullptr ? Json(nullptr) : Handle(town)},
		{"station_allowed", authority.Succeeded()}, {"airport_noise", std::move(noise)}};
	result["feasibility"] = SiteFeasibility(test);
	return Bounded(std::move(result));
}

const Town *TownHandle(const Json &parameters)
{
	const Json &handle = parameters.at("town_id");
	if (!handle.is_object() || handle.size() != 3 || !handle.contains("id") ||
			!handle.contains("generation") || !handle.contains("engine_instance_id") ||
			!handle.at("engine_instance_id").is_string() || handle.at("engine_instance_id").get<std::string>() != InstanceId() ||
			!handle.at("id").is_number_integer() || !handle.at("generation").is_number_integer()) throw InvalidParameters("Invalid town handle");
	Parameters fields(handle);
	auto id = TownID(fields.Int("id", 0, 63999));
	const Town *town = Town::GetIfValid(id);
	if (town == nullptr || (!handle.at("generation").is_number_unsigned() && handle.at("generation").get<int64_t>() <= 0) ||
			town->GetInstanceGeneration() != handle.at("generation").get<uint64_t>()) throw InvalidParameters("Town target is stale or unavailable");
	return town;
}

const char *RatingCategory(int rating)
{
	if (rating > RATING_EXCELLENT) return "outstanding";
	if (rating > RATING_VERYGOOD) return "excellent";
	if (rating > RATING_GOOD) return "very_good";
	if (rating > RATING_MEDIOCRE) return "good";
	if (rating > RATING_POOR) return "mediocre";
	if (rating > RATING_VERYPOOR) return "poor";
	if (rating > RATING_APPALLING) return "very_poor";
	return "appalling";
}

bool TownActionEnabled(TownAction action)
{
	switch (action) {
		case TownAction::RoadRebuild: return _settings_game.economy.fund_roads;
		case TownAction::FundBuildings: return _settings_game.economy.fund_buildings;
		case TownAction::BuyRights: return _settings_game.economy.exclusive_rights;
		case TownAction::Bribe: return _settings_game.economy.bribe;
		default: return true;
	}
}

Json TownAuthority(const Json &parameters)
{
	const Town *town = TownHandle(parameters);
	Json result = SnapshotBase();
	result["town_id"] = Handle(town);
	result["name"] = Text(GetString(STR_TOWN_NAME, town->index));
	result["location"] = TileCoordinates(town->xy);
	result["population"] = town->cache.population;
	result["ratings"] = Json::array();
	for (const Company *company : Company::Iterate()) {
		if (!town->have_ratings.Test(company->index) && town->exclusivity != company->index) continue;
		result["ratings"].push_back({{"company_id", company->index.base()},
			{"category", RatingCategory(town->ratings[company->index])},
			{"exclusive", town->exclusivity == company->index}});
	}
	bool have_rating = town->have_ratings.Test(_current_company) || town->exclusivity == _current_company;
	result["viewer_rating"] = {{"present", have_rating},
		{"category", have_rating ? Json(RatingCategory(town->ratings[_current_company])) : Json(nullptr)}};
	result["exclusivity"] = Company::IsValidID(town->exclusivity) ? Json{{"company_id", town->exclusivity.base()},
		{"remaining_periods", town->exclusive_counter}} : Json(nullptr);
	result["viewer_unwanted_periods"] = town->unwanted[_current_company];
	result["viewer_has_statue"] = town->statues.Test(_current_company);
	result["airport_noise"] = _settings_game.economy.station_noise_level ? Json{{"enabled", true},
		{"current", town->noise_reached}, {"limit", town->MaxTownNoise()}} : Json(nullptr);
	static constexpr std::array<const char *, 8> names = {"advertise_small", "advertise_medium", "advertise_large",
		"road_rebuild", "build_statue", "fund_buildings", "buy_rights", "bribe"};
	TownActions available = GetMaskOfTownActions(_current_company, town);
	result["actions"] = Json::array();
	for (TownAction action : EnumRange(TownAction::End)) {
		Money price = _price[Price::TownAction] * GetTownActionCost(action) >> 8;
		result["actions"].push_back({{"name", names[to_underlying(action)]},
			{"enabled_by_rules", TownActionEnabled(action)}, {"available_now", available.Test(action)},
			{"advisory_cost", price.base()}});
	}
	return Bounded(std::move(result));
}

} // namespace

std::optional<Json> QueryReportsState(const std::string &query, const Json &parameters)
{
	if (query == "world") return WorldReport();
	if (query == "cargo.payment_rates") return CargoPaymentRates(parameters);
	if (query == "catalog.bridges") return BridgeCatalog(parameters);
	if (query == "land.info") return LandInfo(parameters);
	if (query == "station.site") return StationSite(parameters);
	if (query == "town.authority") return TownAuthority(parameters);
	return std::nullopt;
}

} // namespace Integration::Detail
