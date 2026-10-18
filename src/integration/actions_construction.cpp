/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file actions_construction.cpp Normal company construction actions and picker discovery. */

#include "../stdafx.h"
#include "actions_internal.h"
#include "actions_limits.h"
#include "actions_spatial.h"
#include "../3rdparty/monocypher/monocypher.h"
#include "../command_func.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../cargotype.h"
#include "../depot_base.h"
#include "../depot_cmd.h"
#include "../house.h"
#include "../industry.h"
#include "../industry_cmd.h"
#include "../industrytype.h"
#include "../misc/endian_buffer.hpp"
#include "../newgrf_house.h"
#include "../newgrf_industries.h"
#include "../newgrf_object.h"
#include "../newgrf_roadstop.h"
#include "../newgrf_station.h"
#include "../newgrf_text.h"
#include "../object_base.h"
#include "../object_cmd.h"
#include "../object_map.h"
#include "../core/random_func.hpp"
#include "../rail_map.h"
#include "../road_cmd.h"
#include "../road.h"
#include "../road_map.h"
#include "../settings_type.h"
#include "../signs_base.h"
#include "../signs_cmd.h"
#include "../signs_func.h"
#include "../station_base.h"
#include "../station_cmd.h"
#include "../strings_func.h"
#include "../town.h"
#include "../town_cmd.h"
#include "../townname_func.h"
#include "../tree_cmd.h"
#include "../tree_map.h"
#include "../waypoint_base.h"
#include "../waypoint_cmd.h"

#include "table/strings.h"
#include "../table/tree_land.h"

#include "../safeguards.h"

namespace Integration::Detail {
namespace {

bool ConstructionStationAvailable(const StationSpec *spec);
bool ConstructionRoadStopEverAvailable(const RoadStopSpec *spec, StationType type, RoadType road_type);
bool ConstructionRoadStopAvailable(const RoadStopSpec *spec, StationType type, RoadType road_type);

template <typename T, typename Predicate>
const T *ConstructionHandle(const Json &parameters, const char *key, Predicate allowed)
{
	const auto &handle = parameters.at(key);
	if (!handle.is_object() || handle.size() != 3 || !handle.contains("id") || !handle.contains("generation") || !handle.contains("engine_instance_id")) throw InvalidParameters("Entity target requires a complete handle");
	if (handle.at("engine_instance_id").get<std::string>() != InstanceId()) throw InvalidParameters("Entity target belongs to another engine instance");
	Parameters fields(handle);
	using ID = decltype(std::declval<T>().index);
	const T *entity = T::GetIfValid(ID(fields.Int("id", 0, 63999)));
	if (entity == nullptr || !allowed(entity)) throw InvalidParameters("Entity target is unavailable or not permitted");
	if (!handle.at("generation").is_number_unsigned() && !handle.at("generation").is_number_integer()) throw InvalidParameters("Entity generation must be an integer");
	if ((handle.at("generation").is_number_integer() && !handle.at("generation").is_number_unsigned() && handle.at("generation").get<int64_t>() <= 0) ||
			entity->GetInstanceGeneration() != handle.at("generation").get<uint64_t>()) throw InvalidParameters("Entity target is stale");
	return entity;
}

const Town *ConstructionTown(const Json &parameters)
{
	return ConstructionHandle<Town>(parameters, "town_id", [](const Town *) { return true; });
}

const Sign *ConstructionSign(const Json &parameters)
{
	return ConstructionHandle<Sign>(parameters, "sign_id", [](const Sign *sign) { return CompanyCanEditSign(sign); });
}

const Depot *ConstructionDepot(const Json &parameters)
{
	return ConstructionHandle<Depot>(parameters, "depot_id", [](const Depot *depot) { return IsValidTile(depot->xy) && IsTileOwner(depot->xy, _current_company); });
}

template <typename T>
const T *ConstructionLabel(const Json &parameters)
{
	return ConstructionHandle<T>(parameters, "station_id", [](const T *station) { return station->owner == _current_company || station->owner == OWNER_NONE; });
}

template <typename T>
T ConstructionEnum(const Parameters &p, const char *key, std::span<const std::string_view> names, T fallback)
{
	auto text = p.Text(key, std::string(names[static_cast<size_t>(fallback)]));
	auto found = std::ranges::find(names, text);
	if (found == names.end()) throw InvalidParameters(std::string("Invalid selection: ") + key);
	return static_cast<T>(found - names.begin());
}

TownAction ConstructionTownAction(const Parameters &p)
{
	static constexpr std::array<std::string_view, 8> names = {"advertise_small", "advertise_medium", "advertise_large", "road_rebuild", "build_statue", "fund_buildings", "buy_rights", "bribe"};
	return ConstructionEnum(p, "town_action", names, TownAction::AdvertiseSmall);
}

bool ConstructionProspecting(const IndustrySpec *spec)
{
	return spec->IsRawIndustry() && _settings_game.construction.raw_industry_construction == 2;
}

const IndustrySpec *ConstructionIndustry(const Parameters &p)
{
	return GetIndustrySpec(static_cast<IndustryType>(p.Int("industry_type", 0, NUM_INDUSTRYTYPES - 1)));
}

const ObjectSpec *ConstructionObject(const Parameters &p)
{
	auto type = static_cast<ObjectType>(p.Int("object_type", 0, NUM_OBJECTS - 1));
	if (type >= ObjectSpec::Count()) throw InvalidParameters("Object type is unavailable");
	const ObjectSpec *spec = ObjectSpec::Get(type);
	if (type == OBJECT_STATUE || !spec->IsAvailable() || spec->flags.Test(ObjectFlag::OnlyInScenedit)) throw InvalidParameters("Object type is unavailable in normal play");
	if (p.Int("view", 0, 255, 0) >= spec->views) throw InvalidParameters("Object view is unavailable");
	return spec;
}

const HouseSpec *ConstructionHouse(const Parameters &p)
{
	auto type = static_cast<HouseID>(p.Int("house_type", 0, NUM_HOUSES - 1));
	if (type >= HouseSpec::Specs().size()) throw InvalidParameters("House type is unavailable");
	const HouseSpec *spec = HouseSpec::Get(type);
	if (!spec->enabled || !spec->building_flags.Any({BuildingFlag::Size1x1, BuildingFlag::Size2x1, BuildingFlag::Size1x2, BuildingFlag::Size2x2})) throw InvalidParameters("House type is not a selectable building root");
	return spec;
}

const StationClass *ConstructionStationClass(const Parameters &p, bool waypoint)
{
	const auto selected = ResolveRailStationSpecSelection(p, waypoint);
	const auto *cls = StationClass::Get(StationClassID(selected.spec_class));
	if (!ConstructionStationAvailable(cls->GetSpec(selected.spec_index))) throw InvalidParameters("Station specification is currently unavailable");
	return cls;
}

const RoadStopClass *ConstructionRoadStopClass(const Parameters &p, StationType type)
{
	const auto selected = ResolveRoadStopSpecSelection(p, type);
	const auto *cls = RoadStopClass::Get(RoadStopClassID(selected.spec_class));
	if (!ConstructionRoadStopAvailable(cls->GetSpec(selected.spec_index), type, p.Road())) throw InvalidParameters("Road stop specification is currently unavailable");
	return cls;
}

Json ConstructionHQState(bool incarnations = true)
{
	TileIndex tile = Company::Get(_current_company)->location_of_HQ;
	Json state = {{"tile", tile == INVALID_TILE ? Json(nullptr) : Json(tile.base())}};
	if (tile != INVALID_TILE && IsValidTile(tile) && IsTileType(tile, TileType::Object)) {
		const Object *object = Object::GetByTile(tile);
		state["object"] = {{"id", object->index.base()}, {"type", object->type}, {"view", object->view}, {"origin", object->location.tile.base()}, {"width", object->location.w}, {"height", object->location.h}};
		if (incarnations) state["object"]["generation"] = object->GetInstanceGeneration();
	}
	return state;
}

Json ConstructionSignState(const Sign *sign, bool incarnations = true)
{
	return {{"sign_id", incarnations ? Handle(sign) : Json(sign->index.base())}, {"name", sign->name}, {"x", sign->x}, {"y", sign->y}, {"owner", sign->owner.base()}};
}

Json ConstructionLabelState(const BaseStation *station, bool incarnations = true)
{
	return {{"station_id", incarnations ? Handle(station) : Json(station->index.base())}, {"name", station->name}, {"xy", station->xy.base()}, {"owner", station->owner.base()},
		{"spread", {station->spread.tile.base(), station->spread.w, station->spread.h}}};
}

Json ConstructionTownState(const Town *town, TownAction action, bool incarnations = true)
{
	Json state = {{"town_id", incarnations ? Handle(town) : Json(town->index.base())}, {"action", to_underlying(action)}};
	switch (action) {
		case TownAction::RoadRebuild: state["months"] = town->road_build_months; state["allowed"] = _settings_game.economy.fund_roads; break;
		case TownAction::BuildStatue: state["built"] = town->statues.Test(_current_company); break;
		case TownAction::FundBuildings: state["months"] = town->fund_buildings_months; state["allowed"] = _settings_game.economy.fund_buildings; break;
		case TownAction::BuyRights:
			state["exclusive_company"] = town->exclusivity.base(); state["months"] = town->exclusive_counter; state["allowed"] = _settings_game.economy.exclusive_rights; break;
		case TownAction::Bribe:
			state["rating"] = town->ratings[_current_company]; state["unwanted"] = town->unwanted[_current_company]; state["allowed"] = _settings_game.economy.bribe;
			state["exclusive_company"] = town->exclusivity.base(); state["exclusive_months"] = town->exclusive_counter; break;
		default: break;
	}
	return state;
}

Json ConstructionRoadState(TileIndex tile)
{
	Json state = {{"tile", tile.base()}};
	if (IsNormalRoadTile(tile)) state["road"] = {{"bits", GetRoadBits(tile, RoadTramType::Road).base()}, {"tram_bits", GetRoadBits(tile, RoadTramType::Tram).base()}, {"directions", GetDisallowedRoadDirections(tile).base()}};
	return state;
}

template <Commands Tcommand>
auto ConstructionArguments(const CommandDataBuffer &data)
{
	using Args = typename CommandTraits<Tcommand>::Args;
	const auto args = EndianBufferReader::ToValue<Args>(data);
	if (EndianBufferWriter<CommandDataBuffer>::FromValue(args) != data) throw InvalidParameters("Malformed construction command");
	return args;
}

/** Reuse point dependencies while forwarding the original one-point area command. */
template <Commands Tcommand>
CommandDataBuffer ConstructionPointAreaData(const CommandDataBuffer &data)
{
	const auto args = ConstructionArguments<Tcommand>(data);
	const TileIndex tile = std::get<0>(args);
	if (tile != std::get<1>(args) || !IsValidTile(tile)) throw InvalidParameters("Native area construction requires a single valid tile");
	if constexpr (Tcommand == Commands::BuildObjectArea) {
		const ObjectType type = std::get<2>(args);
		if (type >= ObjectSpec::Count() || ObjectSpec::Get(type)->size != OBJECT_SIZE_1X1) throw InvalidParameters("Native area object must be a one-tile specification");
		return EndianBufferWriter<CommandDataBuffer>::FromValue(CommandTraits<Commands::BuildObject>::Args{tile, type, std::get<3>(args)});
	} else {
		static_assert(Tcommand == Commands::PlaceHouseArea);
		const HouseID type = std::get<2>(args);
		if (type >= HouseSpec::Specs().size() || !HouseSpec::Get(type)->building_flags.Test(BuildingFlag::Size1x1)) throw InvalidParameters("Native area house must be a one-tile specification");
		return EndianBufferWriter<CommandDataBuffer>::FromValue(CommandTraits<Commands::PlaceHouse>::Args{tile, type, std::get<3>(args), std::get<4>(args)});
	}
}

} // namespace

StationSpecSelection ResolveRailStationSpecSelection(const Parameters &p, bool waypoint)
{
	auto id = p.Int("spec_class", 0, UINT16_MAX - 1, waypoint ? STAT_CLASS_WAYP.base() : STAT_CLASS_DFLT.base());
	if (static_cast<uint64_t>(id) >= StationClass::GetClassCount()) throw InvalidParameters("Station class is unavailable");
	const auto *cls = StationClass::Get(StationClassID(id));
	if (IsWaypointClass(*cls) != waypoint) throw InvalidParameters("Station class has the wrong kind");
	auto index = p.Int("spec_index", 0, UINT16_MAX - 1, 0);
	if (static_cast<uint64_t>(index) >= cls->GetSpecCount()) throw InvalidParameters("Station specification is unavailable");
	const auto *spec = cls->GetSpec(index);
	if (spec == nullptr && !(index == 0 && cls->Index() == (waypoint ? STAT_CLASS_WAYP : STAT_CLASS_DFLT))) throw InvalidParameters("Station specification is unavailable");
	return {static_cast<uint16_t>(id), static_cast<uint16_t>(index)};
}

StationSpecSelection ResolveRoadStopSpecSelection(const Parameters &p, StationType type)
{
	bool waypoint = type == StationType::RoadWaypoint;
	auto id = p.Int("spec_class", 0, UINT16_MAX - 1, waypoint ? ROADSTOP_CLASS_WAYP.base() : ROADSTOP_CLASS_DFLT.base());
	if (static_cast<uint64_t>(id) >= RoadStopClass::GetClassCount()) throw InvalidParameters("Road stop class is unavailable");
	const auto *cls = RoadStopClass::Get(RoadStopClassID(id));
	if (IsWaypointClass(*cls) != waypoint) throw InvalidParameters("Road stop class has the wrong kind");
	auto index = p.Int("spec_index", 0, UINT16_MAX - 1, 0);
	if (static_cast<uint64_t>(index) >= cls->GetSpecCount()) throw InvalidParameters("Road stop specification is unavailable");
	const auto *spec = cls->GetSpec(index);
	if (spec == nullptr && !(index == 0 && cls->Index() == (waypoint ? ROADSTOP_CLASS_WAYP : ROADSTOP_CLASS_DFLT))) throw InvalidParameters("Road stop specification is unavailable");
	if (!ConstructionRoadStopEverAvailable(spec, type, p.Road())) throw InvalidParameters("Road stop specification is incompatible with the selected type");
	return {static_cast<uint16_t>(id), static_cast<uint16_t>(index)};
}

std::optional<IndustryPlatformDependencies> CaptureIndustryPlatformDependencies(const CommandDataBuffer &data)
{
	const auto args = ConstructionArguments<Commands::BuildIndustry>(data);
	const auto type = std::get<1>(args);
	if (type >= NUM_INDUSTRYTYPES) throw InvalidParameters("Native industry type is unavailable");
	if (ConstructionProspecting(GetIndustrySpec(type))) return std::nullopt;
	if (Map::Size() > NATIVE_MAX_SPATIAL_TILES) throw InvalidParameters("Industry platform map exceeds the native bound");

	/* Native platform leveling runs as OWNER_TOWN with Auto|NoWater. It cannot
	 * remove pooled assets or autoslope them. It can erase normal road stubs,
	 * alter unused rail corners, and clear natural cover. Commit geometry and
	 * human infrastructure, rather than irrelevant growth/animation counters.
	 * No tile procedures, test commands or world writes occur in this scan. */
	crypto_blake2b_ctx hash;
	crypto_blake2b_init(&hash, 32);
	static constexpr std::string_view DOMAIN = "OpenTTD industry platform dependencies v1";
	crypto_blake2b_update(&hash, reinterpret_cast<const uint8_t *>(DOMAIN.data()), DOMAIN.size());
	std::array<uint8_t, 16384> buffer;
	size_t used = 0;
	auto byte = [&](uint8_t value) {
		buffer[used++] = value;
		if (used == buffer.size()) {
			crypto_blake2b_update(&hash, buffer.data(), used);
			used = 0;
		}
	};
	auto integer = [&](uint32_t value) { for (uint shift = 0; shift < 4; ++shift) byte(static_cast<uint8_t>(value >> (8 * shift))); };
	integer(Map::SizeX()); integer(Map::SizeY());
	byte(_settings_game.construction.industry_platform);
	byte(_settings_game.construction.map_height_limit);
	byte(_settings_game.construction.freeform_edges);
	byte(_settings_game.construction.build_on_slopes);
	std::array<bool, MAX_COMPANIES> owners{};
	auto owner = [&](Owner value) {
		byte(value.base());
		if (Company::IsValidID(value)) owners[value.base()] = true;
	};
	for (uint index = 0; index < Map::Size(); ++index) {
		const TileIndex tile(index);
		/* Index order and dimensions give each fixed-width tagged record its
		 * coordinate. Void height storage participates just like native geometry. */
		byte(TileHeight(tile));
		byte(IsNormalRoadTile(tile));
		if (IsNormalRoadTile(tile)) {
			for (RoadTramType rtt : ROADTRAMTYPES_ALL) {
				const RoadType road_type = GetRoadType(tile, rtt);
				byte(road_type);
				byte(road_type == INVALID_ROADTYPE ? 0 : GetRoadBits(tile, rtt).base());
				if (road_type == INVALID_ROADTYPE) byte(OWNER_NONE.base());
				else owner(GetRoadOwner(tile, rtt));
			}
			byte(GetDisallowedRoadDirections(tile).base());
		}
		byte(IsPlainRailTile(tile));
		if (IsPlainRailTile(tile)) {
			byte(GetTrackBits(tile).base());
			byte(GetRailType(tile));
			owner(GetTileOwner(tile));
			byte(GetRailGroundType(tile) == RailGroundType::HalfTileWater);
		}
	}
	crypto_blake2b_update(&hash, buffer.data(), used);
	std::array<uint8_t, 32> digest;
	crypto_blake2b_final(&hash, digest.data());
	static constexpr char HEX[] = "0123456789abcdef";
	std::string hex;
	hex.reserve(digest.size() * 2);
	for (uint8_t value : digest) { hex.push_back(HEX[value >> 4]); hex.push_back(HEX[value & 15]); }
	IndustryPlatformDependencies result{{{"version", 1}, {"digest", std::move(hex)}}, {}};
	for (uint id = 0; id < owners.size(); ++id) if (owners[id]) result.infrastructure_owners.emplace_back(id);
	return result;
}

std::optional<ActionResult> DispatchConstructionAction(const std::string &action, const Json &parameters, ActionInvocation &invocation)
{
	Parameters p(parameters);
	if (action == "object.build") {
		ConstructionObject(p);
		return invocation.Post<Commands::BuildObject>(p.Tile("tile"), ObjectType(p.Int("object_type", 0, NUM_OBJECTS - 1)), static_cast<uint8_t>(p.Int("view", 0, 255, 0)));
	}
	if (action == "house.build") {
		ConstructionHouse(p);
		return invocation.Post<Commands::PlaceHouse>(p.Tile("tile"), HouseID(p.Int("house_type", 0, NUM_HOUSES - 1)), p.Bool("protected"), p.Bool("replace"));
	}
	if (action == "town.found") {
		static constexpr std::array<std::string_view, 4> sizes = {"small", "medium", "large", "random"};
		static constexpr std::array<std::string_view, 5> layouts = {"original", "better_roads", "grid_2x2", "grid_3x3", "random"};
		auto size = ConstructionEnum(p, "size", sizes, TownSize::Small);
		if (size == TownSize::Large) throw InvalidParameters("Large town founding is not normal company gameplay");
		auto layout = ConstructionEnum(p, "layout", layouts, _settings_game.economy.town_layout);
		auto name = p.Text("name");
		uint32_t parts = 0;
		if (name.empty() && !GenerateTownName(_interactive_random, &parts)) throw InvalidParameters("No automatic town name is available");
		return invocation.Post<Commands::FoundTown>(p.Tile("tile"), size, p.Bool("city"), layout, false, parts, name);
	}
	if (action == "town.action") {
		const Town *town = ConstructionTown(parameters);
		return invocation.Post<Commands::TownAction>(town->xy, town->index, ConstructionTownAction(p));
	}
	if (action == "industry.fund") {
		const auto *spec = ConstructionIndustry(p);
		if (!spec->enabled || spec->layouts.empty()) throw InvalidParameters("Industry type is unavailable");
		bool prospect = ConstructionProspecting(spec);
		if (prospect && (parameters.contains("tile") || parameters.contains("layout"))) throw InvalidParameters("Prospecting does not select a tile or layout");
		auto tile = prospect ? TileIndex{} : p.Tile("tile");
		auto layout = static_cast<uint32_t>(p.Int("layout", 0, spec->layouts.size() - 1, 0));
		return invocation.Post<Commands::BuildIndustry>(tile, IndustryType(p.Int("industry_type", 0, NUM_INDUSTRYTYPES - 1)), layout, false, InteractiveRandom());
	}
	if (action == "sign.place") return invocation.Post<Commands::PlaceSign>(p.Tile("tile"), p.Text("name"));
	if (action == "sign.rename" || action == "sign.delete") return invocation.Post<Commands::RenameSign>(ConstructionSign(parameters)->index, action == "sign.delete" ? std::string{} : p.Text("name"), Colours::Invalid);
	if (action == "sign.move") return invocation.Post<Commands::MoveSign>(ConstructionSign(parameters)->index, p.Tile("tile"));
	if (action == "depot.rename") return invocation.Post<Commands::RenameDepot>(ConstructionDepot(parameters)->index, p.Text("name"));
	if (action == "waypoint.rename") return invocation.Post<Commands::RenameWaypoint>(ConstructionLabel<Waypoint>(parameters)->index, p.Text("name"));
	if (action == "station.label.move") return invocation.Post<Commands::MoveStationName>(ConstructionLabel<Station>(parameters)->index, p.Tile("tile"));
	if (action == "waypoint.label.move") return invocation.Post<Commands::MoveWaypointNAme>(ConstructionLabel<Waypoint>(parameters)->index, p.Tile("tile"));
	if (action == "road.waypoint.remove") {
		auto start = p.Tile("start"), end = p.Tile("end");
		BoundedArea(start, end);
		return invocation.Post<Commands::RemoveFromRoadWaypoint>(start, end);
	}
	if (action == "road.pieces.build") {
		static constexpr std::array<std::string_view, 4> directions = {"none", "northbound", "southbound", "both"};
		auto toggle = ConstructionEnum(p, "toggle_directions", directions, uint8_t{0});
		return invocation.Post<Commands::BuildRoad>(p.Tile("tile"), RoadBits(static_cast<uint8_t>(p.Int("road_bits", 1, 15))), p.Road(), DisallowedRoadDirections(toggle), TownID::Invalid());
	}
	if (action == "rail.station.build") {
		const auto *cls = ConstructionStationClass(p, false);
		return invocation.Post<Commands::BuildRailStation>(p.Tile("tile"), p.Rail(), p.GetAxis(), p.Int("tracks", 1, NATIVE_MAX_STATION_EXTENT), p.Int("length", 1, NATIVE_MAX_STATION_EXTENT), cls->Index(), p.Int("spec_index", 0, UINT16_MAX - 1, 0), p.Station("station_id", true), p.Bool("adjacent", true));
	}
	if (action == "rail.waypoint.build") {
		const auto *cls = ConstructionStationClass(p, true);
		return invocation.Post<Commands::BuildRailWaypoint>(p.Tile("tile"), p.GetAxis(), p.Int("width", 1, NATIVE_MAX_STATION_EXTENT, 1), p.Int("height", 1, NATIVE_MAX_STATION_EXTENT, 1), cls->Index(), p.Int("spec_index", 0, UINT16_MAX - 1, 0), p.Station("station_id", true), p.Bool("adjacent", true));
	}
	if (action == "road.stop.build") {
		auto type = p.StopType();
		const auto *cls = ConstructionRoadStopClass(p, type == RoadStopType::Bus ? StationType::Bus : StationType::Truck);
		return invocation.Post<Commands::BuildRoadStop>(p.Tile("tile"), p.Int("width", 1, NATIVE_MAX_STATION_EXTENT, 1), p.Int("length", 1, NATIVE_MAX_STATION_EXTENT, 1), type, p.Bool("drive_through"), p.Direction(), p.Road(), cls->Index(), p.Int("spec_index", 0, UINT16_MAX - 1, 0), p.Station("station_id", true), p.Bool("adjacent", true));
	}
	if (action == "road.waypoint.build") {
		const auto *cls = ConstructionRoadStopClass(p, StationType::RoadWaypoint);
		return invocation.Post<Commands::BuildRoadWaypoint>(p.Tile("tile"), p.GetAxis(), p.Int("width", 1, NATIVE_MAX_STATION_EXTENT, 1), p.Int("height", 1, NATIVE_MAX_STATION_EXTENT, 1), cls->Index(), p.Int("spec_index", 0, UINT16_MAX - 1, 0), p.Station("station_id", true), p.Bool("adjacent", true));
	}
	if (action == "terrain.plant_tree") return invocation.Post<Commands::PlantTree>(p.Tile("tile"), p.Tile("tile"), static_cast<uint8_t>(p.Int("tree_type", 0, TREE_TOYLAND + TREE_COUNT_TOYLAND - 1, TREE_INVALID)), false);
	return std::nullopt;
}

std::optional<Json> CaptureConstructionTargets(const std::string &action, const Json &parameters)
{
	Parameters p(parameters);
	Json state = Json::object();
	if (action == "object.build") {
		const auto *spec = ConstructionObject(p);
		state = {{"type", p.Int("object_type", 0, NUM_OBJECTS - 1)}, {"size", spec->size}, {"views", spec->views}};
		if (p.Int("object_type", 0, NUM_OBJECTS - 1) == OBJECT_HQ) state["hq"] = ConstructionHQState();
	} else if (action == "house.build") {
		const auto *spec = ConstructionHouse(p);
		state = {{"type", p.Int("house_type", 0, NUM_HOUSES - 1)}, {"flags", spec->building_flags.base()}, {"placement", to_underlying(_settings_game.economy.place_houses)}};
	} else if (action == "town.found") {
		state = {{"founding", to_underlying(_settings_game.economy.found_town)}, {"layout", to_underlying(_settings_game.economy.town_layout)}};
	} else if (action == "town.action") {
		state = ConstructionTownState(ConstructionTown(parameters), ConstructionTownAction(p));
	} else if (action == "industry.fund") {
		const auto *spec = ConstructionIndustry(p);
		state = {{"type", p.Int("industry_type", 0, NUM_INDUSTRYTYPES - 1)}, {"layouts", spec->layouts.size()}, {"raw_policy", _settings_game.construction.raw_industry_construction}, {"prospecting", ConstructionProspecting(spec)}};
	} else if (action == "sign.rename" || action == "sign.delete" || action == "sign.move") {
		state = ConstructionSignState(ConstructionSign(parameters));
	} else if (action == "depot.rename") {
		const auto *depot = ConstructionDepot(parameters);
		state = {{"depot_id", Handle(depot)}, {"name", depot->name}, {"tile", depot->xy.base()}};
	} else if (action == "waypoint.rename" || action == "waypoint.label.move") {
		state = ConstructionLabelState(ConstructionLabel<Waypoint>(parameters));
	} else if (action == "station.label.move") {
		state = ConstructionLabelState(ConstructionLabel<Station>(parameters));
	} else if (action == "road.pieces.build") {
		state = ConstructionRoadState(p.Tile("tile"));
	} else {
		return std::nullopt;
	}
	return Json::array({{{"kind", "construction_state"}, {"state", std::move(state)}}});
}

std::optional<Json> ConstructionIntentState(Commands command, const CommandDataBuffer &data)
{
	Json state = Json::object();
	switch (command) {
		case Commands::BuildObjectArea:
			return ConstructionIntentState(Commands::BuildObject, ConstructionPointAreaData<Commands::BuildObjectArea>(data));
		case Commands::PlaceHouseArea:
			return ConstructionIntentState(Commands::PlaceHouse, ConstructionPointAreaData<Commands::PlaceHouseArea>(data));
		case Commands::BuildObject: {
			const auto args = ConstructionArguments<Commands::BuildObject>(data);
			const auto type = std::get<1>(args);
			if (type >= ObjectSpec::Count()) throw InvalidParameters("Native object type is unavailable");
			const auto *spec = ObjectSpec::Get(type);
			state = {{"type", type}, {"size", spec->size}, {"views", spec->views}};
			if (type == OBJECT_HQ) state["hq"] = ConstructionHQState(false);
			break;
		}
		case Commands::PlaceHouse: {
			const auto args = ConstructionArguments<Commands::PlaceHouse>(data);
			const auto type = std::get<1>(args);
			if (type >= HouseSpec::Specs().size()) throw InvalidParameters("Native house type is unavailable");
			state = {{"type", type}, {"flags", HouseSpec::Get(type)->building_flags.base()}, {"placement", to_underlying(_settings_game.economy.place_houses)}};
			break;
		}
		case Commands::FoundTown:
			state = {{"founding", to_underlying(_settings_game.economy.found_town)}, {"layout", to_underlying(_settings_game.economy.town_layout)}};
			break;
		case Commands::TownAction: {
			const auto args = ConstructionArguments<Commands::TownAction>(data);
			const auto *town = Town::GetIfValid(std::get<0>(args));
			if (town == nullptr) throw InvalidParameters("Native town target is unavailable");
			state = ConstructionTownState(town, std::get<1>(args), false);
			break;
		}
		case Commands::BuildIndustry: {
			const auto args = ConstructionArguments<Commands::BuildIndustry>(data);
			const auto type = std::get<1>(args);
			if (type >= NUM_INDUSTRYTYPES) throw InvalidParameters("Native industry type is unavailable");
			const auto *spec = GetIndustrySpec(type);
			state = {{"type", type}, {"layouts", spec->layouts.size()}, {"raw_policy", _settings_game.construction.raw_industry_construction}, {"prospecting", ConstructionProspecting(spec)}};
			break;
		}
		case Commands::RenameSign: {
			const auto args = ConstructionArguments<Commands::RenameSign>(data);
			const auto *sign = Sign::GetIfValid(std::get<0>(args));
			if (sign == nullptr || !CompanyCanEditSign(sign)) throw InvalidParameters("Native sign target is unavailable");
			state = ConstructionSignState(sign, false);
			break;
		}
		case Commands::MoveSign: {
			const auto args = ConstructionArguments<Commands::MoveSign>(data);
			const auto *sign = Sign::GetIfValid(std::get<0>(args));
			if (sign == nullptr || !CompanyCanEditSign(sign)) throw InvalidParameters("Native sign target is unavailable");
			state = ConstructionSignState(sign, false);
			break;
		}
		case Commands::RenameDepot: {
			const auto args = ConstructionArguments<Commands::RenameDepot>(data);
			const auto *depot = Depot::GetIfValid(std::get<0>(args));
			if (depot == nullptr || !IsValidTile(depot->xy) || !IsTileOwner(depot->xy, _current_company)) throw InvalidParameters("Native depot target is unavailable");
			state = {{"depot_id", depot->index.base()}, {"name", depot->name}, {"tile", depot->xy.base()}};
			break;
		}
		case Commands::RenameWaypoint: {
			const auto args = ConstructionArguments<Commands::RenameWaypoint>(data);
			const auto *waypoint = Waypoint::GetIfValid(std::get<0>(args));
			if (waypoint == nullptr || (waypoint->owner != _current_company && waypoint->owner != OWNER_NONE)) throw InvalidParameters("Native waypoint target is unavailable");
			state = ConstructionLabelState(waypoint, false);
			break;
		}
		case Commands::MoveWaypointNAme: {
			const auto args = ConstructionArguments<Commands::MoveWaypointNAme>(data);
			const auto *waypoint = Waypoint::GetIfValid(std::get<0>(args));
			if (waypoint == nullptr || (waypoint->owner != _current_company && waypoint->owner != OWNER_NONE)) throw InvalidParameters("Native waypoint target is unavailable");
			state = ConstructionLabelState(waypoint, false);
			break;
		}
		case Commands::MoveStationName: {
			const auto args = ConstructionArguments<Commands::MoveStationName>(data);
			const auto *station = Station::GetIfValid(std::get<0>(args));
			if (station == nullptr || (station->owner != _current_company && station->owner != OWNER_NONE)) throw InvalidParameters("Native station target is unavailable");
			state = ConstructionLabelState(station, false);
			break;
		}
		case Commands::BuildRoad: {
			const auto args = ConstructionArguments<Commands::BuildRoad>(data);
			state = ConstructionRoadState(std::get<0>(args));
			break;
		}
		default: return std::nullopt;
	}
	return Json{{"construction", std::move(state)}};
}

std::optional<std::vector<TileIndex>> ConstructionIntentTiles(Commands command, const CommandDataBuffer &data)
{
	NativeTileSet tiles;
	auto add = [&](TileIndex tile) {
		if (!IsValidTile(tile)) throw InvalidParameters("Construction dependency is outside the map");
		tiles.insert(tile);
	};
	auto area = [&](TileArea bounds) {
		if (bounds.w == 0 || bounds.h == 0 || bounds.w > Map::SizeX() || bounds.h > Map::SizeY() ||
				TileX(bounds.tile) + bounds.w > Map::SizeX() || TileY(bounds.tile) + bounds.h > Map::SizeY()) throw InvalidParameters("Construction dependency is invalid or exceeds the operational limit");
		for (TileIndex tile : bounds) add(tile);
	};
	switch (command) {
		case Commands::BuildObjectArea:
			return ConstructionIntentTiles(Commands::BuildObject, ConstructionPointAreaData<Commands::BuildObjectArea>(data));
		case Commands::PlaceHouseArea:
			return ConstructionIntentTiles(Commands::PlaceHouse, ConstructionPointAreaData<Commands::PlaceHouseArea>(data));
		case Commands::BuildObject: {
			const auto args = ConstructionArguments<Commands::BuildObject>(data);
			auto type = std::get<1>(args);
			auto view = std::get<2>(args);
			if (type >= ObjectSpec::Count()) throw InvalidParameters("Object type is unavailable");
			const auto *spec = ObjectSpec::Get(type);
			if (view >= spec->views) throw InvalidParameters("Object view is unavailable");
			uint width = GB(spec->size, HasBit(view, 0) ? 4 : 0, 4), height = GB(spec->size, HasBit(view, 0) ? 0 : 4, 4);
			area(TileArea(std::get<0>(args), width, height));
			if (type == OBJECT_HQ) {
				TileIndex old = Company::Get(_current_company)->location_of_HQ;
				if (old != INVALID_TILE && IsValidTile(old) && IsTileType(old, TileType::Object)) area(Object::GetByTile(old)->location);
			}
			break;
		}
		case Commands::PlaceHouse: {
			const auto args = ConstructionArguments<Commands::PlaceHouse>(data);
			auto type = std::get<1>(args);
			if (type >= HouseSpec::Specs().size()) throw InvalidParameters("House type is unavailable");
			const auto *spec = HouseSpec::Get(type);
			uint width = spec->building_flags.Any({BuildingFlag::Size2x1, BuildingFlag::Size2x2}) ? 2 : 1;
			uint height = spec->building_flags.Any({BuildingFlag::Size1x2, BuildingFlag::Size2x2}) ? 2 : 1;
			area(TileArea(std::get<0>(args), width, height));
			/* Root expands normal clear-effects dependencies, including complete
			 * existing multi-tile houses when replace=true or clearing is needed. */
			break;
		}
		case Commands::BuildIndustry: {
			const auto args = ConstructionArguments<Commands::BuildIndustry>(data);
			auto type = std::get<1>(args);
			if (type >= NUM_INDUSTRYTYPES) throw InvalidParameters("Industry type is unavailable");
			const auto *spec = GetIndustrySpec(type);
			if (ConstructionProspecting(spec)) return std::vector<TileIndex>{};
			/* The loader reads at most 255 layouts; unique signed water-check and
			 * unsigned normal byte offsets occupy at most [-128,255]^2. */
			if (spec->layouts.size() > UINT8_MAX) throw InvalidParameters("Industry layout count exceeds the native bound");
			TileIndex anchor = std::get<0>(args);
			add(anchor);
			for (const auto &layout : spec->layouts) {
				if (layout.size() > 384U * 384U) throw InvalidParameters("Industry layout exceeds the native coordinate bound");
				int max_x = 0, max_y = 0;
				for (const auto &entry : layout) {
					if (entry.ti.x < -128 || entry.ti.x > 255 || entry.ti.y < -128 || entry.ti.y > 255) throw InvalidParameters("Industry offset exceeds the native coordinate bound");
					TileIndex tile = TileAddWrap(anchor, entry.ti.x, entry.ti.y);
					/* A native candidate outside the map fails and the engine may try
					 * another layout. Include every valid dependency, not an invented
					 * integration choice of which candidate is feasible. */
					if (IsValidTile(tile)) add(tile);
					if (entry.gfx != GFX_WATERTILE_SPECIALCHECK) { max_x = std::max<int>(max_x, entry.ti.x); max_y = std::max<int>(max_y, entry.ti.y); }
				}
				/* Keep the local site and slope checks exact. This halo is not a
				 * cascade bound: the separate streamed platform proof covers global
				 * heights and human infrastructure without pinning natural growth. */
				uint margin = _settings_game.construction.industry_platform;
				if (margin > 4) throw InvalidParameters("Industry platform margin exceeds the native setting bound");
				if (TileX(anchor) <= margin + 1 || TileY(anchor) <= margin + 1) continue;
				uint x = TileX(anchor) - margin, y = TileY(anchor) - margin;
				uint width = max_x + 2 + 2 * margin, height = max_y + 2 + 2 * margin;
				if (x + width >= Map::MaxX() || y + height >= Map::MaxY()) continue;
				TileArea platform(TileXY(x, y), width, height);
				platform.Expand(3);
				for (TileIndex tile : platform) if (IsValidTile(tile)) add(tile);
			}
			break;
		}
		case Commands::MoveSign: {
			const auto args = ConstructionArguments<Commands::MoveSign>(data);
			const Sign *sign = Sign::GetIfValid(std::get<0>(args));
			if (sign == nullptr) throw InvalidParameters("Sign target is unavailable");
			if (sign->x >= 0 && sign->y >= 0 && static_cast<uint>(sign->x / TILE_SIZE) < Map::SizeX() && static_cast<uint>(sign->y / TILE_SIZE) < Map::SizeY()) add(TileXY(sign->x / TILE_SIZE, sign->y / TILE_SIZE));
			add(std::get<1>(args));
			break;
		}
		case Commands::MoveStationName: {
			const auto args = ConstructionArguments<Commands::MoveStationName>(data);
			const Station *station = Station::GetIfValid(std::get<0>(args));
			if (station == nullptr) throw InvalidParameters("Station target is unavailable");
			add(station->xy); add(std::get<1>(args));
			break;
		}
		case Commands::MoveWaypointNAme: {
			const auto args = ConstructionArguments<Commands::MoveWaypointNAme>(data);
			const Waypoint *waypoint = Waypoint::GetIfValid(std::get<0>(args));
			if (waypoint == nullptr) throw InvalidParameters("Waypoint target is unavailable");
			add(waypoint->xy); add(std::get<1>(args));
			break;
		}
		default: return std::nullopt;
	}
	return std::vector<TileIndex>(tiles.begin(), tiles.end());
}

} // namespace Integration::Detail

namespace Integration::Detail {
namespace {

/** Bound public names without splitting a UTF-8 code point. */
std::string ConstructionCatalogName(std::string value)
{
	if (value.size() <= 128) return value;
	size_t end = 128;
	while (end > 0 && (static_cast<unsigned char>(value[end]) & 0xC0) == 0x80) --end;
	value.resize(end);
	return value;
}

/** The normal rail picker availability predicate; no tile or RNG is needed. */
bool ConstructionStationAvailable(const StationSpec *spec)
{
	if (spec == nullptr || !spec->callback_mask.Test(StationCallbackMask::Avail)) return true;
	uint16_t result = GetStationCallback(CBID_STATION_AVAILABILITY, 0, 0, spec, nullptr, INVALID_TILE);
	return result == CALLBACK_FAILED || Convert8bitBooleanCallback(spec->grf_prop.grffile, CBID_STATION_AVAILABILITY, result);
}

/** Compatibility filtering used by the road picker before its callback. */
bool ConstructionRoadStopEverAvailable(const RoadStopSpec *spec, StationType type, RoadType road_type)
{
	if (spec == nullptr) return true;
	if (spec->flags.Test(RoadStopSpecFlag::RoadOnly) && !RoadTypeIsRoad(road_type)) return false;
	if (spec->flags.Test(RoadStopSpecFlag::TramOnly) && !RoadTypeIsTram(road_type)) return false;
	switch (spec->stop_type) {
		case ROADSTOPTYPE_ALL: return true;
		case ROADSTOPTYPE_PASSENGER: return type == StationType::Bus;
		case ROADSTOPTYPE_FREIGHT: return type == StationType::Truck;
		default: return false;
	}
}

/** The normal road picker availability predicate with an explicit road type. */
bool ConstructionRoadStopAvailable(const RoadStopSpec *spec, StationType type, RoadType road_type)
{
	if (!ConstructionRoadStopEverAvailable(spec, type, road_type)) return false;
	if (spec == nullptr || !spec->callback_mask.Test(RoadStopCallbackMask::Avail)) return true;
	uint16_t result = GetRoadStopCallback(CBID_STATION_AVAILABILITY, 0, 0, spec, nullptr, INVALID_TILE, road_type, type, 0);
	return result == CALLBACK_FAILED || Convert8bitBooleanCallback(spec->grf_prop.grffile, CBID_STATION_AVAILABILITY, result);
}

/** Resolve the same prototype name as the house picker without a town/tile. */
StringID PrototypeHouseName(const HouseSpec *spec)
{
	std::array<int32_t, 1> regs100;
	uint16_t result = GetHouseCallback(CBID_HOUSE_CUSTOM_NAME, 1, 0, spec->Index(), nullptr, INVALID_TILE, regs100, true);
	if (result != CALLBACK_FAILED && result != 0x400 && spec->grf_prop.HasGrfFile()) {
		StringID name = STR_NULL;
		if (result == 0x40F) {
			name = GetGRFStringID(spec->grf_prop.grffile->grfid, static_cast<GRFStringID>(regs100[0]));
		} else if (result < 0x400) {
			name = GetGRFStringID(spec->grf_prop.grffile->grfid, GRFSTR_MISC_GRF_TEXT + result);
		}
		if (name != STR_NULL && name != STR_UNDEFINED) return name;
	}
	return spec->building_name;
}

/** Page configuration indices with the same limits as ordinary pool pages. */
template <typename Filter, typename Serialize>
Json ConstructionCatalogPage(const Parameters &p, uint64_t count, Filter filter, Serialize serialize)
{
	uint64_t index = static_cast<uint64_t>(p.Int("after_id", -1, UINT32_MAX, -1) + 1);
	size_t limit = static_cast<size_t>(p.Int("limit", 1, 256, 64));
	Json items = Json::array();
	uint scanned = 0;
	while (index < count && scanned < 4096 && items.size() < limit) {
		if (filter(index)) items.push_back(serialize(index));
		++index;
		++scanned;
	}
	return {{"items", std::move(items)}, {"next_after_id", index < count ? Json(index - 1) : Json(nullptr)}};
}

/**
 * Page class/spec pairs without flattening potentially large NewGRF catalogs.
 * A class transition consumes scan work and skips its nonexistent tail slots.
 */
template <typename Class, typename ClassFilter, typename SpecFilter, typename Serialize>
Json ConstructionClassSpecPage(const Parameters &p, ClassFilter class_filter, SpecFilter spec_filter, Serialize serialize)
{
	uint64_t next = static_cast<uint64_t>(p.Int("after_id", -1, UINT32_MAX, -1) + 1);
	uint64_t class_index = next >> 16;
	uint64_t spec_index = next & UINT16_MAX;
	uint64_t last_examined = next;
	size_t limit = static_cast<size_t>(p.Int("limit", 1, 256, 64));
	Json items = Json::array();
	uint scanned = 0;
	auto classes = Class::Classes();
	while (class_index < classes.size() && scanned < 4096 && items.size() < limit) {
		const auto &cls = classes[class_index];
		/* Wire spec indices use 0..65534, matching the reviewed action schema. */
		uint64_t count = std::min<uint64_t>(cls.GetSpecCount(), UINT16_MAX);
		if (!class_filter(cls) || spec_index >= count) {
			last_examined = (class_index << 16) | UINT16_MAX;
			++class_index;
			spec_index = 0;
			++scanned;
			continue;
		}
		uint16_t index = static_cast<uint16_t>(spec_index);
		last_examined = (class_index << 16) | spec_index;
		++spec_index;
		++scanned;
		const auto *spec = cls.GetSpec(index);
		/* Only the two native index-zero choices are represented by null. */
		bool native_default = index == 0 && cls.Index().base() <= 1;
		if ((spec != nullptr || native_default) && spec_filter(spec)) items.push_back(serialize(cls, index, spec, last_examined));
	}
	return {{"items", std::move(items)}, {"next_after_id", class_index < classes.size() ? Json(last_examined) : Json(nullptr)}};
}

std::pair<uint, uint> ConstructionHouseDimensions(const HouseSpec *spec)
{
	return {spec->building_flags.Any({BuildingFlag::Size2x1, BuildingFlag::Size2x2}) ? 2U : 1U,
		spec->building_flags.Any({BuildingFlag::Size1x2, BuildingFlag::Size2x2}) ? 2U : 1U};
}

bool ConstructionIndustryListed(const IndustrySpec *spec)
{
	return spec->enabled && !spec->layouts.empty() &&
			(!spec->IsRawIndustry() || _settings_game.construction.raw_industry_construction != 0);
}

Json ConstructionCargoIDs(const auto &cargoes)
{
	Json ids = Json::array();
	CargoTypes seen{};
	for (auto cargo : cargoes) {
		if (cargo >= NUM_CARGO || !CargoSpec::Get(cargo)->IsValid() || seen.Test(cargo)) continue;
		seen.Set(cargo);
		ids.push_back(to_underlying(cargo));
	}
	return ids;
}

VehicleType ConstructionDepotVehicleType(const Depot *depot)
{
	if (IsRailDepotTile(depot->xy)) return VehicleType::Train;
	if (IsRoadDepotTile(depot->xy)) return VehicleType::Road;
	if (IsShipDepotTile(depot->xy)) return VehicleType::Ship;
	return VehicleType::Invalid;
}

TileIndex ConstructionSignTile(const Sign *sign)
{
	if (sign->x < 0 || sign->y < 0) return INVALID_TILE;
	uint x = sign->x / TILE_SIZE;
	uint y = sign->y / TILE_SIZE;
	return x < Map::SizeX() && y < Map::SizeY() ? TileXY(x, y) : INVALID_TILE;
}

Json ConstructionOwnerCompany(Owner owner)
{
	return owner < MAX_COMPANIES ? Json(owner.base()) : Json(nullptr);
}

} // namespace

std::optional<Json> QueryConstructionState(const std::string &query, const Json &parameters)
{
	if (query != "catalog.objects" && query != "catalog.houses" && query != "catalog.industries" && query != "catalog.trees" &&
			query != "catalog.station_specs" && query != "catalog.roadstop_specs" && query != "depots" && query != "waypoints" && query != "signs") return std::nullopt;
	RequireCompany();
	Parameters p(parameters);
	Json result = SnapshotBase();
	if (query == "catalog.objects") {
		result.update(ConstructionCatalogPage(p, ObjectSpec::Count(), [](uint64_t id) {
			return id != OBJECT_STATUE && ObjectSpec::Get(static_cast<ObjectType>(id))->IsAvailable();
		}, [](uint64_t id) {
			const ObjectSpec *spec = ObjectSpec::Get(static_cast<ObjectType>(id));
			uint width = GB(spec->size, 0, 4), height = GB(spec->size, 4, 4);
			uint cost_tiles = id < NEW_OBJECT_OFFSET ? 1U : width * height;
			return Json{{"object_type", id}, {"name", ConstructionCatalogName(GetString(spec->name))}, {"width", width}, {"height", height},
				{"views", spec->views}, {"base_build_cost", (spec->GetBuildCost() * cost_tiles).base()}, {"site_costs_extra", true},
				{"removable", !spec->flags.Test(ObjectFlag::CannotRemove)}, {"built_on_water", spec->flags.Any({ObjectFlag::BuiltOnWater, ObjectFlag::NotOnLand})},
				{"water_only", spec->flags.Test(ObjectFlag::NotOnLand)}};
		}));
	} else if (query == "catalog.houses") {
		HouseZones climate = GetClimateMaskForLandscape();
		result.update(ConstructionCatalogPage(p, HouseSpec::Specs().size(), [climate](uint64_t id) {
			const HouseSpec *spec = HouseSpec::Get(id);
			return spec->enabled && spec->building_flags.Any(BUILDING_HAS_1_TILE) && spec->building_availability.Any(climate) && spec->building_availability.Any(HZ_ZONE_ALL);
		}, [](uint64_t id) {
			const HouseSpec *spec = HouseSpec::Get(id);
			auto [width, height] = ConstructionHouseDimensions(spec);
			return Json{{"house_type", id}, {"name", ConstructionCatalogName(GetString(PrototypeHouseName(spec)))}, {"width", width}, {"height", height},
				{"population", spec->population}, {"introduction_year", spec->min_year.base()}, {"last_year", spec->max_year.base()},
				{"placement_enabled", _settings_game.economy.place_houses != PlaceHouses::Forbidden}, {"protected_option", true}};
		}));
	} else if (query == "catalog.industries") {
		result.update(ConstructionCatalogPage(p, NUM_INDUSTRYTYPES, [](uint64_t id) {
			return ConstructionIndustryListed(GetIndustrySpec(static_cast<IndustryType>(id)));
		}, [](uint64_t id) {
			const IndustrySpec *spec = GetIndustrySpec(static_cast<IndustryType>(id));
			return Json{{"industry_type", id}, {"name", ConstructionCatalogName(GetString(spec->name))}, {"cost", spec->GetConstructionCost().base()},
				{"mode", spec->IsRawIndustry() && _settings_game.construction.raw_industry_construction == 2 ? "prospect" : "fund"},
				{"layout_count", spec->layouts.size()}, {"accepts_cargo_ids", ConstructionCargoIDs(spec->accepts_cargo)},
				{"produces_cargo_ids", ConstructionCargoIDs(spec->produced_cargo)},
				{"available", GetIndustryProbabilityCallback(static_cast<IndustryType>(id), IndustryAvailabilityCallType::UserCreation, 1) > 0}};
		}));
	} else if (query == "catalog.trees") {
		auto landscape = to_underlying(_settings_game.game_creation.landscape);
		uint base = _tree_base_by_landscape[landscape], count = _tree_count_by_landscape[landscape];
		result.update(ConstructionCatalogPage(p, base + count, [base](uint64_t id) { return id >= base; }, [base](uint64_t id) {
			const bool rainforest = id >= TREE_RAINFOREST && id < TREE_CACTUS;
			const bool cactus = id == TREE_CACTUS;
			StringID name = rainforest ? STR_LAI_TREE_NAME_RAINFOREST : cactus ? STR_LAI_TREE_NAME_CACTUS_PLANTS : STR_LAI_TREE_NAME_TREES;
			Json zone = nullptr;
			if (_settings_game.game_creation.landscape == LandscapeType::Tropic) zone = rainforest ? "rainforest" : cactus ? "desert" : "normal";
			return Json{{"tree_type", id}, {"name", ConstructionCatalogName(GetString(name))}, {"variant", id - base + 1}, {"required_tropic_zone", std::move(zone)}};
		}));
	} else if (query == "catalog.station_specs") {
		std::string kind = p.Text("kind", "station");
		if (kind != "station" && kind != "waypoint") throw InvalidParameters("Invalid station kind");
		const bool waypoint = kind == "waypoint";
		result.update(ConstructionClassSpecPage<StationClass>(p, [waypoint](const auto &cls) { return IsWaypointClass(cls) == waypoint; },
			[](const StationSpec *) { return true; }, [waypoint](const auto &cls, uint16_t index, const StationSpec *spec, uint64_t id) {
				StringID name = spec != nullptr ? spec->name : waypoint ? STR_STATION_CLASS_WAYP_WAYPOINT : STR_STATION_CLASS_DFLT_STATION;
				return Json{{"catalog_id", id}, {"spec_class", cls.Index().base()}, {"spec_index", index},
					{"class_name", ConstructionCatalogName(GetString(cls.name))}, {"name", ConstructionCatalogName(GetString(name))},
					{"available", ConstructionStationAvailable(spec)}, {"disallowed_platforms", spec != nullptr ? spec->disallowed_platforms : 0},
					{"disallowed_lengths", spec != nullptr ? spec->disallowed_lengths : 0}};
			}));
	} else if (query == "catalog.roadstop_specs") {
		std::string kind = p.Text("kind", "bus");
		if (kind != "bus" && kind != "truck" && kind != "waypoint") throw InvalidParameters("Invalid road stop kind");
		RoadType road_type = p.Road();
		if (!Company::Get(_current_company)->avail_roadtypes.Test(road_type)) throw InvalidParameters("Road type is unavailable");
		const bool waypoint = kind == "waypoint";
		StationType type = waypoint ? StationType::RoadWaypoint : kind == "truck" ? StationType::Truck : StationType::Bus;
		result.update(ConstructionClassSpecPage<RoadStopClass>(p, [waypoint](const auto &cls) { return IsWaypointClass(cls) == waypoint; },
			[type, road_type](const RoadStopSpec *spec) { return ConstructionRoadStopEverAvailable(spec, type, road_type); },
			[waypoint, type, road_type](const auto &cls, uint16_t index, const RoadStopSpec *spec, uint64_t id) {
				StringID name = spec != nullptr ? spec->name : waypoint ? STR_STATION_CLASS_WAYP_WAYPOINT : STR_STATION_CLASS_DFLT_ROADSTOP;
				return Json{{"catalog_id", id}, {"spec_class", cls.Index().base()}, {"spec_index", index},
					{"class_name", ConstructionCatalogName(GetString(cls.name))}, {"name", ConstructionCatalogName(GetString(name))},
					{"available", ConstructionRoadStopAvailable(spec, type, road_type)},
					{"drive_through_only", waypoint || RoadTypeIsTram(road_type) || (spec != nullptr && spec->flags.Test(RoadStopSpecFlag::DriveThroughOnly))},
					{"road_only", spec != nullptr && spec->flags.Test(RoadStopSpecFlag::RoadOnly)},
					{"tram_only", spec != nullptr && spec->flags.Test(RoadStopSpecFlag::TramOnly)}};
			}));
	} else if (query == "depots") {
		result.update(PoolPage<Depot>(p, [](const Depot *depot) {
			return IsValidTile(depot->xy) && GetTileOwner(depot->xy) == _current_company && ConstructionDepotVehicleType(depot) != VehicleType::Invalid;
		}, [](const Depot *depot) {
			VehicleType type = ConstructionDepotVehicleType(depot);
			return Json{{"depot_id", Handle(depot)}, {"name", ConstructionCatalogName(GetString(STR_DEPOT_NAME, type, depot->index))},
				{"location", TileCoordinates(depot->xy)}, {"vehicle_type", VehicleTypeName(type)}};
		}));
	} else if (query == "waypoints") {
		result.update(PoolPage<Waypoint>(p, [](const Waypoint *waypoint) {
			return (waypoint->owner == _current_company || waypoint->owner == OWNER_NONE) && IsValidTile(waypoint->xy);
		}, [](const Waypoint *waypoint) {
			const char *kind = HasBit(waypoint->waypoint_flags, WPF_ROAD) ? "road" : waypoint->string_id == STR_SV_STNAME_BUOY ? "buoy" : "rail";
			return Json{{"station_id", Handle(waypoint)}, {"name", ConstructionCatalogName(GetString(STR_WAYPOINT_NAME, waypoint->index))},
				{"location", TileCoordinates(waypoint->xy)}, {"kind", kind}, {"owner_company_id", ConstructionOwnerCompany(waypoint->owner)}};
		}));
	} else if (query == "signs") {
		result.update(PoolPage<Sign>(p, [](const Sign *sign) { return IsValidTile(ConstructionSignTile(sign)); }, [](const Sign *sign) {
			return Json{{"sign_id", Handle(sign)}, {"name", ConstructionCatalogName(GetString(STR_SIGN_NAME, sign->index))},
				{"location", TileCoordinates(ConstructionSignTile(sign))}, {"owner_company_id", ConstructionOwnerCompany(sign->owner)}, {"editable", CompanyCanEditSign(sign)}};
		}));
	}
	return result;
}

} // namespace Integration::Detail
