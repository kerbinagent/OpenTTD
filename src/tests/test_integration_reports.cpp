/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file test_integration_reports.cpp Native preview fidelity regressions. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions_internal.h"
#include "../clear_map.h"
#include "../cargotype.h"
#include "../core/endian_func.hpp"
#include "../core/random_func.hpp"
#include "../industry.h"
#include "../industry_map.h"
#include "../industrytype.h"
#include "../language.h"
#include "../newgrf_config.h"
#include "../newgrf_storage.h"
#include "../newgrf_text.h"
#include "../rail.h"
#include "../station_func.h"
#include "../strings_func.h"
#include "../town.h"
#include "../window_func.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <thread>

#include "table/strings.h"
#include "../command_func.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../depot_base.h"
#include "../newgrf_roadstop.h"
#include "../newgrf_station.h"
#include "../road.h"
#include "../road_map.h"
#include "../station_base.h"
#include "../station_cmd.h"
#include "../station_type.h"

#include "../safeguards.h"

namespace {

using namespace Integration;
using namespace Integration::Detail;

void WaitForSiteGate() { std::this_thread::sleep_for(std::chrono::milliseconds(1050)); }

struct ReportsWorld {
	CompanyID previous_company = _current_company;
	uint8_t previous_spread = _settings_game.station.station_spread;
	TileIndex tile;

	ReportsWorld()
	{
		if (_current_language == nullptr) {
			static LanguageMetadata language;
			language.file = std::filesystem::path("lang") / "english.lng";
			std::ifstream file(language.file, std::ios::binary);
			file.read(reinterpret_cast<char *>(static_cast<LanguagePackHeader *>(&language)), sizeof(LanguagePackHeader));
			REQUIRE(file.good());
			REQUIRE(language.IsValid());
			language.missing = FROM_LE16(language.missing);
			language.winlangid = FROM_LE16(language.winlangid);
			REQUIRE(ReadLanguagePack(&language));
		}
		_industry_pool.CleanPool();
		_town_pool.CleanPool();
		_persistent_storage_pool.CleanPool();
		_depot_pool.CleanPool();
		_station_pool.CleanPool();
		_company_pool.CleanPool();
		Map::Allocate(128, 128);
		StationClass::Reset();
		RoadStopClass::Reset();
		ResetRoadTypes();
		ResetRailTypes();
		_settings_game.station.station_spread = 16;
		REQUIRE(Company::CanAllocateItem());
		_current_company = Company::Create()->index;
		Company::Get(_current_company)->avail_railtypes.Set(RAILTYPE_RAIL);
		Company::Get(_current_company)->avail_roadtypes.Set(ROADTYPE_ROAD);
		for (uint y = 0; y < Map::SizeY(); ++y) {
			for (uint x = 0; x < Map::SizeX(); ++x) {
				TileIndex candidate = TileXY(x, y);
				if (IsInnerTile(candidate)) MakeClear(candidate, ClearGround::Grass, 3);
			}
		}
		tile = TileXY(20, 20);
		REQUIRE(Depot::CanAllocateItem());
		Depot *depot = Depot::Create(tile);
		MakeRoadDepot(tile, _current_company, depot->index, DiagDirection::NE, ROADTYPE_ROAD);
	}

	~ReportsWorld()
	{
		_industry_pool.CleanPool();
		_town_pool.CleanPool();
		_persistent_storage_pool.CleanPool();
		StationClass::Reset();
		RoadStopClass::Reset();
		_depot_pool.CleanPool();
		_station_pool.CleanPool();
		_company_pool.CleanPool();
		_current_company = previous_company;
		_settings_game.station.station_spread = previous_spread;
	}
};

} // namespace

TEST_CASE("Station site preview uses normal build test flags on an owned road depot", "[integration][reports]")
{
	ReportsWorld world;
	const auto normal_flags = CommandFlagsToDCFlags(GetCommandFlags<Commands::BuildRoadStop>());
	REQUIRE(normal_flags.Test(DoCommandFlag::Auto));
	const CommandCost normal = Command<Commands::BuildRoadStop>::Do(normal_flags,
		world.tile, uint8_t{1}, uint8_t{1}, RoadStopType::Bus, false, DiagDirection::NE,
		ROADTYPE_ROAD, ROADSTOP_CLASS_DFLT, uint16_t{0}, NEW_STATION, true);
	REQUIRE(normal.Failed());

	Integration::Json parameters = {{"build_action", "road.stop.build"}, {"build_parameters", {
		{"tile", world.tile.base()}, {"stop_type", "bus"}, {"drive_through", false},
		{"direction", to_underlying(DiagDirection::NE)}, {"width", 1}, {"length", 1}}}};
	WaitForSiteGate();
	const auto preview = Integration::Detail::QueryReportsState("station.site", parameters);
	REQUIRE(preview.has_value());
	CHECK((*preview)["feasibility"]["status"] == "query_test_failed");
	CHECK((*preview)["feasibility"]["error"]["code"] == "query_test_failed");
	CHECK(IsRoadDepotTile(world.tile));
}

TEST_CASE("Station site admits native extent64 with both rail axes and road geometry", "[integration][reports][station-site]")
{
	ReportsWorld world;
	_settings_game.station.station_spread = 64;
	const TileIndex anchor = TileXY(40, 40);
	const auto random = _random, interactive = _interactive_random;
	const auto stations = BaseStation::GetNumItems(), depots = Depot::GetNumItems();
	const auto station_generation = BaseStation::GetLastInstanceGeneration();
	const auto money = Company::Get(_current_company)->money;
	for (Axis axis : {Axis::X, Axis::Y}) {
		Json parameters = {{"build_action", "rail.station.build"}, {"build_parameters", {
			{"tile", anchor.base()}, {"axis", axis == Axis::X ? "x" : "y"}, {"tracks", 2}, {"length", 64}}}};
		const auto native = Command<Commands::BuildRailStation>::Do(
			CommandFlagsToDCFlags(GetCommandFlags<Commands::BuildRailStation>()) | DoCommandFlag::QueryCost,
			anchor, RAILTYPE_RAIL, axis, uint8_t{2}, uint8_t{64}, STAT_CLASS_DFLT, uint16_t{0}, NEW_STATION, true);
		REQUIRE(native.Succeeded());
		WaitForSiteGate();
		const auto preview = QueryReportsState("station.site", parameters);
		REQUIRE(preview.has_value());
		CHECK(preview->at("footprint").at("width") == (axis == Axis::X ? 64 : 2));
		CHECK(preview->at("footprint").at("height") == (axis == Axis::X ? 2 : 64));
		CHECK(preview->at("feasibility").at("cost") == native.GetCost().base());
		CHECK(preview->at("feasibility").at("status") == "query_test_passed");
		CHECK_THROWS_AS(QueryReportsState("station.site", parameters), InvalidParameters);
	}
	Json road = {{"build_action", "road.stop.build"}, {"build_parameters", {
		{"tile", anchor.base()}, {"stop_type", "bus"}, {"drive_through", true}, {"direction", 0}, {"width", 64}, {"length", 2}}}};
	const auto native = Command<Commands::BuildRoadStop>::Do(
		CommandFlagsToDCFlags(GetCommandFlags<Commands::BuildRoadStop>()) | DoCommandFlag::QueryCost,
		anchor, uint8_t{64}, uint8_t{2}, RoadStopType::Bus, true, DiagDirection::NE,
		ROADTYPE_ROAD, ROADSTOP_CLASS_DFLT, uint16_t{0}, NEW_STATION, true);
	REQUIRE(native.Succeeded());
	WaitForSiteGate();
	const auto preview = QueryReportsState("station.site", road);
	REQUIRE(preview.has_value());
	CHECK(preview->at("footprint").at("width") == 64);
	CHECK(preview->at("footprint").at("height") == 2);
	CHECK(preview->at("feasibility").at("cost") == native.GetCost().base());
	/* The API admits normal extent64; the current native setting still decides feasibility. */
	_settings_game.station.station_spread = 16;
	WaitForSiteGate();
	const auto failed = QueryReportsState("station.site", road);
	REQUIRE(failed.has_value());
	CHECK(failed->at("feasibility").at("status") == "query_test_failed");
	road["build_parameters"]["width"] = 65;
	CHECK_THROWS_AS(QueryReportsState("station.site", road), InvalidParameters);
	road["build_parameters"]["width"] = 64;
	road["build_parameters"]["tile"] = TileXY(100, 100).base();
	CHECK_THROWS_AS(QueryReportsState("station.site", road), InvalidParameters);
	CHECK(BaseStation::GetNumItems() == stations);
	CHECK(Depot::GetNumItems() == depots);
	CHECK(BaseStation::GetLastInstanceGeneration() == station_generation);
	CHECK(Company::Get(_current_company)->money == money);
	CHECK(_random.state[0] == random.state[0]);
	CHECK(_random.state[1] == random.state[1]);
	CHECK(_interactive_random.state[0] == interactive.state[0]);
	CHECK(_interactive_random.state[1] == interactive.state[1]);
	CHECK(IsTileType(anchor, TileType::Clear));
}

TEST_CASE("Station site selected specifications use native availability once without query diagnostics", "[integration][reports][station-site]")
{
	ReportsWorld world;
	GRFFile grf;
	grf.grfid = GrfID{"\x21\x43\x65\x7F"};
	grf.grf_version = 8;
	StationSpec rail;
	rail.grf_prop.SetGRFFile(&grf);
	rail.class_index = StationClass::Allocate(StationClass::GlobalID{"TEST"});
	StationClass::Assign(&rail);
	RoadStopSpec road;
	road.grf_prop.SetGRFFile(&grf);
	road.class_index = RoadStopClass::Allocate(RoadStopClass::GlobalID{"TEST"});
	RoadStopClass::Assign(&road);
	/* A malformed v8 boolean would enter ErrorUnknownCallbackResult without a
	 * registered config. QueryCost must suppress that whole diagnostic path. */
	REQUIRE(SpriteGroup::CanAllocateItem());
	std::unique_ptr<CallbackResultSpriteGroup> invalid(CallbackResultSpriteGroup::Create(2));
	rail.grf_prop.SetSpriteGroup(CargoGRFFileProps::SG_DEFAULT, invalid.get());
	rail.callback_mask.Set(StationCallbackMask::Avail);
	road.grf_prop.SetSpriteGroup(CargoGRFFileProps::SG_DEFAULT, invalid.get());
	road.callback_mask.Set(RoadStopCallbackMask::Avail);
	Json parameters = {{"build_action", "rail.station.build"}, {"build_parameters", {
		{"tile", TileXY(40, 40).base()}, {"axis", "x"}, {"tracks", 2}, {"length", 3},
		{"spec_class", rail.class_index.base()}, {"spec_index", rail.index}}}};
	const auto selected = ResolveRailStationSpecSelection(Parameters(parameters.at("build_parameters")), false);
	CHECK(selected.spec_class == rail.class_index.base());
	CHECK(selected.spec_index == rail.index);
	const auto normal = Command<Commands::BuildRailStation>::Do(
		CommandFlagsToDCFlags(GetCommandFlags<Commands::BuildRailStation>()) | DoCommandFlag::QueryCost,
		TileXY(40, 40), RAILTYPE_RAIL, Axis::X, uint8_t{2}, uint8_t{3}, rail.class_index, rail.index, NEW_STATION, true);
	REQUIRE(normal.Succeeded());
	WaitForSiteGate();
	const auto preview = QueryReportsState("station.site", parameters);
	REQUIRE(preview.has_value());
	CHECK(preview->at("selection").at("spec_class") == rail.class_index.base());
	CHECK(preview->at("selection").at("spec_index") == rail.index);
	CHECK(preview->at("feasibility").at("cost") == normal.GetCost().base());
	CHECK(FindWindowById(WindowClass::ErrorMessage, 0) == nullptr);
	/* The no-station availability resolver has no valid tile or parent town. */
	RoadStopResolverObject resolver(&road, nullptr, INVALID_TILE, ROADTYPE_ROAD, StationType::Bus, 0);
	CHECK(resolver.GetTown() == nullptr);
	Json stop = {{"build_action", "road.stop.build"}, {"build_parameters", {
		{"tile", TileXY(40, 40).base()}, {"stop_type", "bus"}, {"drive_through", false}, {"direction", 0},
		{"spec_class", road.class_index.base()}, {"spec_index", road.index}}}};
	WaitForSiteGate();
	const auto road_preview = QueryReportsState("station.site", stop);
	REQUIRE(road_preview.has_value());
	CHECK(road_preview->at("selection").at("spec_class") == road.class_index.base());
	CHECK(road_preview->at("feasibility").at("status") == "query_test_passed");
	CHECK(FindWindowById(WindowClass::ErrorMessage, 0) == nullptr);
	road.flags.Set(RoadStopSpecFlag::TramOnly);
	CHECK_THROWS_AS(QueryReportsState("station.site", stop), InvalidParameters);
	road.flags.Reset(RoadStopSpecFlag::TramOnly);
	road.stop_type = ROADSTOPTYPE_FREIGHT;
	CHECK_THROWS_AS(QueryReportsState("station.site", stop), InvalidParameters);
	parameters["build_parameters"]["spec_class"] = STAT_CLASS_WAYP.base();
	CHECK_THROWS_AS(QueryReportsState("station.site", parameters), InvalidParameters);
	parameters["build_parameters"]["spec_class"] = rail.class_index.base();
	parameters["build_parameters"]["spec_index"] = rail.index + 1;
	CHECK_THROWS_AS(QueryReportsState("station.site", parameters), InvalidParameters);
	/* The silent conversion preserves v7 truncation and v8 nonzero semantics. */
	CHECK_FALSE(Convert8bitBooleanCallback(&grf, CBID_STATION_AVAILABILITY, 0, false));
	CHECK(Convert8bitBooleanCallback(&grf, CBID_STATION_AVAILABILITY, 2, false));
	grf.grf_version = 7;
	CHECK_FALSE(Convert8bitBooleanCallback(&grf, CBID_STATION_AVAILABILITY, 0x100, false));
	CHECK(Convert8bitBooleanCallback(&grf, CBID_STATION_AVAILABILITY, 1, false));
}

TEST_CASE("Station site max rectangle returns every cargo with bounded escaped UTF8 previews", "[integration][reports][station-site]")
{
	ReportsWorld world;
	_settings_game.station.station_spread = 64;
	struct RestoreSpecs {
		std::array<CargoSpec, NUM_CARGO> cargo;
		std::array<IndustryTileSpec, 4> tiles;
		RestoreSpecs()
		{
			for (CargoType c : EnumRange(NUM_CARGO)) this->cargo[c] = *CargoSpec::Get(c);
			for (size_t i = 0; i < this->tiles.size(); ++i) this->tiles[i] = _industry_tile_specs[i];
		}
		~RestoreSpecs()
		{
			for (CargoType c : EnumRange(NUM_CARGO)) *CargoSpec::Get(c) = std::move(this->cargo[c]);
			for (size_t i = 0; i < this->tiles.size(); ++i) _industry_tile_specs[i] = std::move(this->tiles[i]);
		}
	} restore;
	const GrfID grfid{"\x21\x43\x65\x7E"};
	/* Quotes and backslashes are the worst printable JSON expansion (2x).
	 * A multibyte codepoint straddles byte64 in the second label. */
	const std::string escaped(128, '"');
	const StringID escaped_name = AddGRFString(grfid, GRFSTR_MISC_GRF_TEXT,
		static_cast<uint8_t>(GRFLanguage::English), true, true, escaped, STR_UNDEFINED);
	const StringID utf8_name = AddGRFString(grfid, GRFSTR_MISC_GRF_TEXT + 1,
		static_cast<uint8_t>(GRFLanguage::English), true, true,
		std::string(63, '\\') + "\xC3\xA9" + "\n\t tail", STR_UNDEFINED);
	for (CargoType c : EnumRange(NUM_CARGO)) {
		CargoSpec *spec = CargoSpec::Get(c);
		spec->bitnum = to_underlying(c);
		spec->label = CT_PASSENGERS;
		spec->name = c == 1 ? utf8_name : escaped_name;
		REQUIRE(spec->IsValid());
	}
	for (uint i = 0; i < 4; ++i) {
		const TileIndex tile = TileXY(42 + i, 42);
		REQUIRE(Industry::CanAllocateItem());
		Industry *industry = Industry::Create(tile);
		industry->type = 0;
		industry->location.w = 1;
		industry->location.h = 1;
		industry->produced.resize(INDUSTRY_NUM_OUTPUTS);
		auto &tile_spec = _industry_tile_specs[i];
		tile_spec.callback_mask = {};
		tile_spec.special_flags = {};
		tile_spec.grf_prop.override_id = INVALID_INDUSTRYTILE;
		tile_spec.accepts_cargo.fill(INVALID_CARGO);
		tile_spec.acceptance.fill(0);
		for (uint j = 0; j < INDUSTRY_NUM_INPUTS; ++j) {
			const auto cargo = static_cast<CargoType>(i * INDUSTRY_NUM_INPUTS + j);
			tile_spec.accepts_cargo[j] = cargo;
			tile_spec.acceptance[j] = 8;
			industry->produced[j].cargo = cargo;
		}
		MakeIndustry(tile, industry->index, i, 0, WaterClass::Invalid);
	}
	Json parameters = {{"build_action", "rail.station.build"}, {"build_parameters", {
		{"tile", TileXY(40, 40).base()}, {"axis", "x"}, {"tracks", 64}, {"length", 64}}}};
	const auto psa_count = PersistentStorage::GetNumItems();
	const auto psa_generation = PersistentStorage::GetLastInstanceGeneration();
	WaitForSiteGate();
	const auto started = std::chrono::steady_clock::now();
	const auto preview = QueryReportsState("station.site", parameters);
	const auto elapsed = std::chrono::steady_clock::now() - started;
	INFO("max ordinary station.site microseconds=" << std::chrono::duration_cast<std::chrono::microseconds>(elapsed).count());
	REQUIRE(preview.has_value());
	CHECK(preview->at("footprint").at("tile_count") == 4096);
	REQUIRE(preview->at("accepted_cargo").size() == NUM_CARGO);
	REQUIRE(preview->at("supplied_cargo").size() == NUM_CARGO);
	CHECK(preview->at("accepted_cargo").at(0).at("name") == std::string(64, '"'));
	CHECK(preview->at("accepted_cargo").at(1).at("name") == std::string(63, '\\'));
	for (const char *key : {"accepted_cargo", "supplied_cargo"}) {
		for (const Json &item : preview->at(key)) {
			CHECK(item.at("name").get_ref<const std::string &>().size() <= 64);
			CHECK(item.at("name_truncated").get<bool>());
			CHECK(item.at("name").get_ref<const std::string &>().find_first_of("\n\r\t") == std::string::npos);
		}
	}
	CHECK(preview->dump().size() < 40 * 1024);
	/* Serialize the actual complete DTO with worst printable JSON escaping,
	 * maximal native counters and a full1024byte feasibility error. */
	Json worst = *preview;
	worst["tick"] = UINT64_MAX;
	worst["company_id"] = 14;
	for (Json &item : worst["accepted_cargo"]) {
		item["name"] = std::string(64, '"');
		item["acceptance_eighths"] = UINT32_MAX;
	}
	for (Json &item : worst["supplied_cargo"]) {
		item["name"] = std::string(64, '\\');
		item["source_count"] = UINT32_MAX;
	}
	worst["feasibility"]["advisory_tick"] = UINT64_MAX;
	worst["feasibility"]["error"] = {{"code", "query_test_failed"}, {"message", std::string(1024, '"')}};
	worst["authority"]["town_id"] = {{"id", 63999}, {"generation", UINT64_MAX}, {"engine_instance_id", std::string(256, '\\')}};
	worst["authority"]["airport_noise"] = {{"enabled", true}, {"current", UINT32_MAX}, {"increment", UINT32_MAX}, {"limit", UINT32_MAX}};
	CHECK(worst.dump().size() < 40 * 1024);
	CHECK(PersistentStorage::GetNumItems() == psa_count);
	CHECK(PersistentStorage::GetLastInstanceGeneration() == psa_generation);
	/* Preserve the native negative-acceptance rule rather than invent a clamp. */
	_industry_tile_specs[0].acceptance[0] = -1;
	const auto [acceptance, always] = GetAcceptanceAroundTiles(TileXY(40, 40), 64, 64, 4);
	CHECK(acceptance[0] == 0);
	CHECK_FALSE(always.Test(static_cast<CargoType>(0)));
}
