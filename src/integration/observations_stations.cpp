/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file observations_stations.cpp Bounded public station and cargo-routing observations. */

#include "../stdafx.h"
#include "actions_internal.h"
#include "../3rdparty/monocypher/monocypher.h"
#include "../cargotype.h"
#include "../company_base.h"
#include "../core/math_func.hpp"
#include "../linkgraph/linkgraph.h"
#include "../map_func.h"
#include "../settings_type.h"
#include "../station_base.h"
#include "../station_cmd.h"
#include "../strings_func.h"
#include "../timer/timer_game_tick.h"
#include "../town.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <iterator>
#include <limits>
#include <map>
#include <new>
#include <tuple>

#include "table/strings.h"

#include "../safeguards.h"

namespace Integration::Detail {
namespace {

constexpr size_t STATION_REPORT_BYTES = 40 * 1024;
constexpr size_t STATION_NAME_BYTES = 128;
constexpr size_t STATION_PAGE_ROWS = 64;
constexpr size_t LINK_SCAN_LIMIT = 256;

const char *DistributionModeName(DistributionType mode)
{
	switch (mode) {
		case DistributionType::Manual: return "manual";
		case DistributionType::Asymmetric: return "asymmetric";
		case DistributionType::Symmetric: return "symmetric";
		default: throw InvalidParameters("Invalid cargo distribution mode");
	}
}

Json BoundedStationReport(Json result)
{
	if (result.dump().size() > STATION_REPORT_BYTES) throw InvalidParameters("Station report exceeds the observation byte budget");
	return result;
}

/** Native strings are UTF-8. Never cut a multibyte code point in the public label. */
std::string ShortStationName(std::string value)
{
	if (value.size() <= STATION_NAME_BYTES) return value;
	size_t length = STATION_NAME_BYTES;
	while (length > 0 && (static_cast<unsigned char>(value[length]) & 0xc0) == 0x80) --length;
	value.resize(length);
	return value;
}

/** Observation reads use human Station View visibility, not Parameters::Station's action-owner guard. */
const Station *VisibleStation(const Json &parameters)
{
	const auto &value = parameters.at("station_id");
	if (!value.is_object() || !value.contains("id") || !value.contains("generation") || !value.contains("engine_instance_id")) throw InvalidParameters("Invalid station handle");
	if (value.at("engine_instance_id").get_ref<const std::string &>() != InstanceId()) throw InvalidParameters("Station target belongs to another engine instance");
	const auto raw_id = value.at("id").get<uint32_t>();
	if (raw_id >= StationID::Invalid().base()) throw InvalidParameters("Station target is invalid");
	const Station *station = Station::GetIfValid(StationID(raw_id));
	if (station == nullptr || station->GetInstanceGeneration() != value.at("generation").get<uint64_t>()) throw InvalidParameters("Station target is stale");
	return station;
}

const GoodsEntry &CargoEntry(const Station *station, const Parameters &parameters, CargoType &cargo)
{
	cargo = static_cast<CargoType>(parameters.Int("cargo_id", 0, NUM_CARGO - 1));
	if (!CargoSpec::Get(cargo)->IsValid()) throw InvalidParameters("Cargo is unavailable");
	return station->goods[cargo];
}

Json CurrentStationHandle(StationID id)
{
	const Station *station = Station::GetIfValid(id);
	return station == nullptr ? Json(nullptr) : Handle(station);
}

const char *DirectionName(Direction direction)
{
	switch (direction) {
		case Direction::N: return "north";
		case Direction::E: return "east";
		case Direction::S: return "south";
		case Direction::W: return "west";
		default: return "north";
	}
}

Json StationDetails(const Json &parameters)
{
	const Station *station = VisibleStation(parameters);
	Json result = SnapshotBase();
	result["station"] = Handle(station);
	result["name"] = ShortStationName(GetString(STR_STATION_NAME, station->index));
	result["owner"] = Company::IsValidID(station->owner) ? Handle(Company::Get(station->owner)) : Json(nullptr);
	result["sign_tile"] = station->xy == INVALID_TILE ? Json(nullptr) : Json(station->xy.base());
	result["town"] = station->town == nullptr ? Json(nullptr) : Handle(station->town);
	result["facilities"] = Json::array();
	for (const auto &[facility, name] : {std::pair{StationFacility::Train, "train"}, {StationFacility::BusStop, "bus"},
			{StationFacility::TruckStop, "truck"}, {StationFacility::Airport, "airport"}, {StationFacility::Dock, "dock"}}) {
		if (station->facilities.Test(facility)) result["facilities"].push_back(name);
	}
	result["spread"] = station->spread.IsEmpty() ? Json(nullptr) : Json{{"x", TileX(station->spread.tile)},
		{"y", TileY(station->spread.tile)}, {"width", station->spread.w}, {"height", station->spread.h}};
	result["catchment_radius"] = station->GetCatchmentRadius();
	result["airport"] = nullptr;
	if (station->facilities.Test(StationFacility::Airport) && !station->airport.IsEmpty()) {
		result["airport"] = { {"type_id", station->airport.type}, {"layout", station->airport.layout},
			{"rotation", DirectionName(station->airport.rotation)},
			{"closed", station->airport.blocks.Test(AirportBlock::AirportClosed)},
			{"hangars", station->airport.GetNumHangars()} };
	}
	result["cargo"] = Json::array();
	for (CargoType cargo : EnumRange(NUM_CARGO)) {
		if (!CargoSpec::Get(cargo)->IsValid()) continue;
		const GoodsEntry &goods = station->goods[cargo];
		const uint available = goods.HasData() ? goods.GetData().cargo.AvailableCount() : 0;
		const uint reserved = goods.HasData() ? goods.GetData().cargo.ReservedCount() : 0;
		Json monthly_supply = nullptr;
		const LinkGraph *graph = LinkGraph::GetIfValid(goods.link_graph);
		if (graph != nullptr && graph->Cargo() == cargo && goods.node < graph->Size() && (*graph)[goods.node].station == station->index) {
			monthly_supply = graph->Monthly((*graph)[goods.node].supply);
		}
		result["cargo"].push_back({{"cargo_id", to_underlying(cargo)},
			{"accepts", goods.status.Test(GoodsEntry::State::Acceptance)},
			{"has_rating", goods.HasRating()},
			{"rating_percent", goods.HasRating() ? Json(ToPercent8(goods.rating)) : Json(nullptr)},
			{"waiting", {{"available", available}, {"reserved", reserved}, {"total", goods.TotalCount()}}},
			{"monthly_supply", monthly_supply}});
	}
	return BoundedStationReport(std::move(result));
}

Json StationCargoFlows(const Json &parameters)
{
	Parameters p(parameters);
	const Station *station = VisibleStation(parameters);
	CargoType cargo;
	const GoodsEntry &goods = CargoEntry(station, p, cargo);
	const int64_t after_source = p.Int("after_source_id", -1, StationID::Invalid().base(), -1);
	const uint32_t after_cumulative = static_cast<uint32_t>(p.Int("after_share_cumulative", 0, UINT32_MAX, 0));
	if (parameters.contains("after_share_cumulative") && after_source < 0) throw InvalidParameters("Flow cursor requires a source");
	const size_t limit = static_cast<size_t>(p.Int("limit", 1, STATION_PAGE_ROWS, 50));
	Json result = SnapshotBase();
	result["station"] = Handle(station);
	result["cargo_id"] = to_underlying(cargo);
	result["unit"] = "planned_flow_per_economy_month";
	result["has_cargo_data"] = goods.HasData();
	result["distribution_mode"] = DistributionModeName(_settings_game.linkgraph.GetDistributionType(cargo));
	result["rows"] = Json::array();
	result["next"] = nullptr;
	result["complete"] = true;
	if (!goods.HasData()) {
		if (after_source >= 0) throw InvalidParameters("Flow cursor is stale");
		return BoundedStationReport(std::move(result));
	}
	const FlowStatMap &flows = goods.GetData().flows;
	auto source = after_source < 0 ? flows.begin() : flows.lower_bound(StationID(static_cast<uint16_t>(after_source)));
	if (after_source >= 0) {
		if (source == flows.end() || source->first.base() != after_source ||
			(after_cumulative != 0 && !source->second.GetShares()->contains(after_cumulative))) throw InvalidParameters("Flow cursor is stale");
	}
	FlowStat::SharesMap::const_iterator share{};
	if (source != flows.end()) share = source->second.GetShares()->upper_bound(after_cumulative);
	while (source != flows.end()) {
		const auto &shares = *source->second.GetShares();
		if (share == shares.end()) {
			++source;
			if (source == flows.end()) break;
			share = source->second.GetShares()->begin();
			continue;
		}
		if (result["rows"].size() == limit) break;
		const uint32_t previous = share == shares.begin() ? 0 : std::prev(share)->first;
		const StationID via = share->second;
		const bool routed = via != StationID::Invalid();
		Json row = {{"source_station_id", source->first.base()},
			{"source", CurrentStationHandle(source->first)},
			{"via_station_id", routed ? Json(via.base()) : Json(nullptr)},
			{"via", routed ? CurrentStationHandle(via) : Json(nullptr)},
			{"amount", share->first - previous},
			{"restricted", share->first > source->second.GetUnrestricted()},
			{"local_consumption", routed && via == station->index}};
		/* The instance identifier is bounded by the central schema but may be long. */
		if (result.dump().size() + row.dump().size() + 256 > STATION_REPORT_BYTES) {
			if (result["rows"].empty()) throw InvalidParameters("A flow row exceeds the observation byte budget");
			break;
		}
		result["rows"].push_back(std::move(row));
		result["next"] = {{"after_source_id", source->first.base()}, {"after_share_cumulative", share->first}};
		++share;
	}
	result["complete"] = source == flows.end();
	if (result["complete"].get<bool>()) result["next"] = nullptr;
	return BoundedStationReport(std::move(result));
}

using WaitingKey = std::tuple<uint16_t, uint16_t, uint16_t>; // first station, normalized next hop, packet units

struct WaitingBin {
	uint32_t packet_count = 0;
	uint64_t units = 0;
};

/** Fixed-width, little-endian digest for complete histogram pagination. */
class WaitingDigest {
	crypto_blake2b_ctx hash{};

public:
	WaitingDigest() { crypto_blake2b_init(&this->hash, 32); }

	void Bytes(const void *data, size_t size)
	{
		crypto_blake2b_update(&this->hash, static_cast<const uint8_t *>(data), size);
	}

	void Number(uint64_t value, uint8_t width)
	{
		std::array<uint8_t, 8> bytes{};
		for (uint8_t i = 0; i < width; ++i) bytes[i] = static_cast<uint8_t>(value >> (8 * i));
		this->Bytes(bytes.data(), width);
	}

	void Text(std::string_view value)
	{
		if (value.size() > UINT32_MAX) throw InvalidParameters("Engine instance identifier is too long");
		this->Number(value.size(), 4);
		this->Bytes(value.data(), value.size());
	}

	void StationHandle(StationID id)
	{
		const Station *station = Station::GetIfValid(id);
		this->Number(station != nullptr, 1);
		if (station != nullptr) this->Number(station->GetInstanceGeneration(), 8);
	}

	std::string Finish()
	{
		std::array<uint8_t, 32> digest{};
		crypto_blake2b_final(&this->hash, digest.data());
		constexpr char HEX[] = "0123456789abcdef";
		std::string result = "swh1:";
		result.reserve(result.size() + digest.size() * 2);
		for (uint8_t byte : digest) {
			result += HEX[byte >> 4];
			result += HEX[byte & 0x0f];
		}
		return result;
	}
};

Json WaitingCursor(const WaitingKey &key, bool manual)
{
	return {{"first_station_id", std::get<0>(key)}, {"next_hop_id", manual ? Json(nullptr) : Json(std::get<1>(key))},
		{"packet_units", std::get<2>(key)}};
}

/** Exact packet-size histogram, without estimating destinations or reading flow maps. */
Json StationCargoWaiting(const Json &parameters)
{
	try {
		Parameters p(parameters);
		const Station *station = VisibleStation(parameters);
		CargoType cargo;
		const GoodsEntry &goods = CargoEntry(station, p, cargo);
		const size_t limit = static_cast<size_t>(p.Int("limit", 1, STATION_PAGE_ROWS, 32));
		const DistributionType distribution = _settings_game.linkgraph.GetDistributionType(cargo);
		const char *distribution_name = DistributionModeName(distribution);
		const bool manual = distribution == DistributionType::Manual;

		const bool has_cursor = parameters.contains("after_group");
		const bool check_version = parameters.contains("aggregate_version");
		if (check_version && !has_cursor) throw InvalidParameters("Waiting aggregate version requires a cursor");
		std::optional<WaitingKey> after_group;
		std::string supplied_version;
		if (has_cursor) {
			const Json &cursor = parameters.at("after_group");
			if (!cursor.is_object() || cursor.size() != 3 || !cursor.contains("first_station_id") ||
					!cursor.contains("next_hop_id") || !cursor.contains("packet_units")) throw InvalidParameters("Invalid waiting cursor");
			Parameters c(cursor);
			const auto first = static_cast<uint16_t>(c.Int("first_station_id", 0, StationID::Invalid().base()));
			uint16_t next;
			if (manual) {
				if (!cursor.at("next_hop_id").is_null()) throw InvalidParameters("Manual waiting cursor must suppress next hop");
				next = StationID::Invalid().base();
			} else {
				next = static_cast<uint16_t>(c.Int("next_hop_id", 0, StationID::Invalid().base()));
			}
			const auto packet_units = static_cast<uint16_t>(c.Int("packet_units", 1, CargoPacket::MAX_COUNT));
			after_group.emplace(first, next, packet_units);
		}
		if (check_version) {
			if (!parameters.at("aggregate_version").is_string()) throw InvalidParameters("Invalid waiting aggregate version");
			supplied_version = parameters.at("aggregate_version").get<std::string>();
			if (supplied_version.size() != 69 || supplied_version.compare(0, 5, "swh1:") != 0 ||
					!std::all_of(supplied_version.begin() + 5, supplied_version.end(), [](char ch) {
						return (ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f');
					})) throw InvalidParameters("Invalid waiting aggregate version");
		}

		/* One admitted full walk per monotonic second, globally across actors and pages. */
		static std::optional<std::chrono::steady_clock::time_point> last_start;
		const auto now = std::chrono::steady_clock::now();
		if (last_start.has_value() && now - *last_start < std::chrono::seconds(1)) throw InvalidParameters("Station waiting aggregate rate limit");
		last_start = now;

		std::map<WaitingKey, WaitingBin> bins;
		uint64_t available_units = 0;
		uint32_t native_available_cached = 0;
		uint32_t native_reserved_cached = 0;
		uint32_t native_total_cached = 0;
		if (goods.HasData()) {
			const StationCargoList &packets = goods.GetData().cargo;
			for (StationCargoList::ConstIterator it = packets.Packets()->begin(); it != packets.Packets()->end(); ++it) {
				const CargoPacket *packet = *it;
				if (packet == nullptr || packet->Count() == 0) throw InvalidParameters("Invalid waiting cargo packet");
				const uint16_t packet_units = packet->Count();
				const WaitingKey key{packet->GetFirstStation().base(), manual ? StationID::Invalid().base() : it.GetKey().base(), packet_units};
				WaitingBin &bin = bins[key];
				if (bin.packet_count == UINT32_MAX || bin.units > UINT64_MAX - packet_units || available_units > UINT64_MAX - packet_units) throw InvalidParameters("Waiting cargo count overflow");
				++bin.packet_count;
				bin.units += packet_units;
				available_units += packet_units;
			}
			native_available_cached = packets.AvailableCount();
			native_reserved_cached = packets.ReservedCount();
			native_total_cached = packets.TotalCount();
		}
		const bool available_matches_native_cache = available_units == native_available_cached;
		const bool native_total_addition_fits_u32 = static_cast<uint64_t>(native_available_cached) + native_reserved_cached <= UINT32_MAX;

		WaitingDigest digest;
		static constexpr char DOMAIN[] = "OpenTTD station waiting histogram v1";
		digest.Bytes(DOMAIN, sizeof(DOMAIN)); // Include the zero terminator.
		digest.Text(InstanceId());
		digest.Number(station->index.base(), 2);
		digest.Number(station->GetInstanceGeneration(), 8);
		digest.Number(to_underlying(cargo), 1);
		digest.Number(to_underlying(distribution), 1);
		digest.Number(manual, 1);
		digest.Number(available_units, 8);
		digest.Number(native_available_cached, 4);
		digest.Number(native_reserved_cached, 4);
		digest.Number(native_total_cached, 4);
		digest.Number(available_matches_native_cache, 1);
		digest.Number(native_total_addition_fits_u32, 1);
		digest.Number(bins.size(), 8);
		for (const auto &[key, bin] : bins) {
			const auto [first, next, packet_units] = key;
			const bool source_visible_in_gui = !manual || first != station->index.base();
			digest.Number(first, 2);
			digest.Number(next, 2);
			digest.Number(packet_units, 2);
			digest.Number(bin.packet_count, 4);
			digest.Number(bin.units, 8);
			digest.Number(source_visible_in_gui, 1);
			digest.StationHandle(StationID(first));
			if (manual) {
				digest.Number(0, 1);
			} else {
				digest.StationHandle(StationID(next));
			}
		}
		const std::string version = digest.Finish();
		if (check_version && supplied_version != version) throw InvalidParameters("Station waiting aggregate changed; restart pagination");

		Json result = SnapshotBase();
		result["station"] = Handle(station);
		result["cargo_id"] = to_underlying(cargo);
		result["distribution_mode"] = distribution_name;
		result["route_details_suppressed"] = manual;
		result["destination_semantics"] = "not_estimated";
		result["available_units"] = available_units;
		result["native_available_cached"] = native_available_cached;
		result["native_reserved_cached"] = native_reserved_cached;
		result["native_total_cached"] = native_total_cached;
		result["available_matches_native_cache"] = available_matches_native_cache;
		result["native_total_addition_fits_u32"] = native_total_addition_fits_u32;
		result["aggregate_version"] = version;
		result["live"] = true;
		result["rows"] = Json::array();
		result["next_after_group"] = nullptr;
		result["complete"] = true;
		auto it = after_group.has_value() ? bins.upper_bound(*after_group) : bins.begin();
		std::optional<WaitingKey> last_emitted;
		for (; it != bins.end() && result["rows"].size() < limit; ++it) {
			const auto &[first, next, packet_units] = it->first;
			const WaitingBin &bin = it->second;
			Json row = {{"first_station_id", first}, {"first_station", CurrentStationHandle(StationID(first))},
				{"source_visible_in_gui", !manual || first != station->index.base()},
				{"next_hop_id", manual ? Json(nullptr) : Json(next)},
				{"next_hop", manual ? Json(nullptr) : CurrentStationHandle(StationID(next))},
				{"packet_units", packet_units}, {"packet_count", bin.packet_count}, {"units", bin.units}};
			result["rows"].push_back(std::move(row));
			/* Check with a worst-case continuation envelope; the final response can only be smaller. */
			result["next_after_group"] = WaitingCursor(it->first, manual);
			result["complete"] = false;
			if (result.dump().size() > STATION_REPORT_BYTES) {
				result["rows"].erase(std::prev(result["rows"].end()));
				if (result["rows"].empty()) throw InvalidParameters("A waiting histogram row exceeds the observation byte budget");
				break;
			}
			last_emitted = it->first;
		}
		result["complete"] = it == bins.end();
		if (result["complete"].get<bool>()) result["next_after_group"] = nullptr;
		else result["next_after_group"] = WaitingCursor(*last_emitted, manual);
		return BoundedStationReport(std::move(result));
	} catch (const std::bad_alloc &) {
		throw InvalidParameters("Station waiting aggregate exhausted memory");
	}
}

Json StationLinks(const Json &parameters)
{
	Parameters p(parameters);
	const Station *station = VisibleStation(parameters);
	CargoType cargo;
	const GoodsEntry &goods = CargoEntry(station, p, cargo);
	const int64_t after_offset = p.Int("after_offset", -1, 65535, -1);
	const size_t limit = static_cast<size_t>(p.Int("limit", 1, STATION_PAGE_ROWS, 50));
	Json result = SnapshotBase();
	result["station"] = Handle(station);
	result["cargo_id"] = to_underlying(cargo);
	result["rows"] = Json::array();
	result["next_after_offset"] = nullptr;
	result["complete"] = true;
	const LinkGraph *graph = LinkGraph::GetIfValid(goods.link_graph);
	if (graph == nullptr || graph->Cargo() != cargo || goods.node >= graph->Size() || (*graph)[goods.node].station != station->index) return BoundedStationReport(std::move(result));
	const auto &edges = (*graph)[goods.node].edges;
	size_t offset = static_cast<size_t>(after_offset + 1);
	if (offset > edges.size()) throw InvalidParameters("Link cursor is outside the current graph");
	size_t inspected = 0;
	while (offset < edges.size() && inspected < LINK_SCAN_LIMIT && result["rows"].size() < limit) {
		const auto &edge = edges[offset];
		++offset;
		++inspected;
		if (edge.dest_node >= graph->Size() || edge.capacity == 0) continue;
		const StationID destination_id = (*graph)[edge.dest_node].station;
		const Station *destination = Station::GetIfValid(destination_id);
		if (destination == nullptr) continue;
		const GoodsEntry &destination_goods = destination->goods[cargo];
		if (destination_goods.link_graph != goods.link_graph || destination_goods.node != edge.dest_node) continue;
		result["rows"].push_back({{"to", Handle(destination)},
			{"monthly_capacity", graph->Monthly(edge.capacity)},
			{"monthly_usage", graph->Monthly(edge.usage)},
			{"travel_days", edge.TravelTime() / Ticks::DAY_TICKS},
			{"shared_with_neutral", station->owner == OWNER_NONE || destination->owner == OWNER_NONE}});
	}
	result["complete"] = offset == edges.size();
	if (!result["complete"].get<bool>()) result["next_after_offset"] = offset - 1;
	return BoundedStationReport(std::move(result));
}

} // namespace

std::optional<Json> QueryStationDetailsState(const std::string &query, const Json &parameters)
{
	if (query == "station.details") return StationDetails(parameters);
	if (query == "station.cargo.flows") return StationCargoFlows(parameters);
	if (query == "station.cargo.waiting") return StationCargoWaiting(parameters);
	if (query == "station.links") return StationLinks(parameters);
	return std::nullopt;
}

} // namespace Integration::Detail
