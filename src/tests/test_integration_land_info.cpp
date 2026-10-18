/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file test_integration_land_info.cpp Native land information callback isolation. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions_internal.h"
#include "../cargotype.h"
#include "../cheat_type.h"
#include "../clear_map.h"
#include "../command_func.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../core/backup_type.hpp"
#include "../core/endian_func.hpp"
#include "../core/random_func.hpp"
#include "../depot_base.h"
#include "../economy_func.h"
#include "../genworld.h"
#include "../house.h"
#include "../industry.h"
#include "../industry_map.h"
#include "../industrytype.h"
#include "../landscape.h"
#include "../landscape_cmd.h"
#include "../language.h"
#include "../newgrf_callbacks.h"
#include "../newgrf_config.h"
#include "../newgrf_industries.h"
#include "../newgrf_spritegroup.h"
#include "../newgrf_storage.h"
#include "../newgrf_text.h"
#include "../openttd.h"
#include "../settings_type.h"
#include "../station_base.h"
#include "../station_func.h"
#include "../strings_func.h"
#include "../tile_cmd.h"
#include "../town.h"
#include "../town_map.h"
#include "../void_map.h"
#include "../window_func.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <thread>

#include "table/strings.h"
#include "../safeguards.h"

namespace {

using namespace Integration;
using namespace Integration::Detail;

struct LandInfoWorld {
	AutoRestoreBackup<CompanyID> company_backup{_current_company};
	AutoRestoreBackup<GameMode> mode_backup{_game_mode, GameMode::Normal};
	AutoRestoreBackup<bool> generating_backup{_generating_world, false};
	AutoRestoreBackup<bool> bulldozer_backup{_cheats.magic_bulldozer.value, false};
	std::vector<HouseSpec> old_houses = HouseSpec::Specs();
	IndustrySpec old_industry = _industry_specs[0];
	IndustryTileSpec old_industry_tile = _industry_tile_specs[0];
	CargoSpec old_cargo = *CargoSpec::Get(0);
	Money old_house_price = _price[Price::ClearHouse];
	size_t old_configs = _grfconfig.size();
	GRFFile grf;
	GRFConfig *config;
	Company *company;
	Town *town;
	TileIndex house_tile;
	std::vector<std::unique_ptr<SpriteGroup>> groups;

	LandInfoWorld()
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
		_persistent_storage_pool.CleanPool();
		_industry_pool.CleanPool();
		_station_pool.CleanPool();
		_town_pool.CleanPool();
		_company_pool.CleanPool();
		Map::Allocate(128, 128);
		for (Tile tile : Map::Iterate()) {
			if (IsInnerTile(tile)) MakeClear(tile, ClearGround::Grass, 3);
			else MakeVoid(tile);
		}
		house_tile = TileXY(20, 20);
		REQUIRE(Company::CanAllocateItem());
		company = Company::Create();
		_current_company = company->index;
		company->money = 100000;
		company->clear_limit = 10 << 16;
		REQUIRE(Town::CanAllocateItem());
		town = Town::Create(TileXY(10, 10));
		town->ratings[company->index] = 500;
		RebuildTownKdtree();
		grf.grfid = GrfID{"\x4C\x41\x4E\x44"};
		grf.grf_version = 8;
		grf.cargo_list = {CT_PASSENGERS};
		auto cfg = std::make_unique<GRFConfig>("land-info-test.grf");
		cfg->ident.grfid = grf.grfid;
		config = cfg.get();
		_grfconfig.push_back(std::move(cfg));
		CargoSpec::Get(0)->bitnum = 0;
		CargoSpec::Get(0)->label = CT_PASSENGERS;
		BuildCargoLabelMap();
		HouseSpec::Specs().resize(std::max<size_t>(1, HouseSpec::Specs().size()));
		auto &house = *HouseSpec::Get(0);
		house = HouseSpec{};
		house.grf_prop.SetGRFFile(&grf);
		house.grf_prop.override_id = INVALID_HOUSE_ID;
		house.building_name = STR_CARGO_PLURAL_PASSENGERS;
		house.removal_cost = 1;
		house.remove_rating_decrease = 10;
		std::fill(std::begin(house.accepts_cargo), std::end(house.accepts_cargo), INVALID_CARGO);
		house.accepts_cargo[0] = static_cast<CargoType>(0);
		_price[Price::ClearHouse] = 256;
	}

	~LandInfoWorld()
	{
		HouseSpec::Specs() = std::move(old_houses);
		_industry_specs[0] = std::move(old_industry);
		_industry_tile_specs[0] = std::move(old_industry_tile);
		*CargoSpec::Get(0) = std::move(old_cargo);
		BuildCargoLabelMap();
		_price[Price::ClearHouse] = old_house_price;
		groups.clear();
		_grfconfig.resize(old_configs);
		_persistent_storage_pool.CleanPool();
		_industry_pool.CleanPool();
		_station_pool.CleanPool();
		_town_pool.CleanPool();
		_company_pool.CleanPool();
		RebuildTownKdtree();
	}

	const SpriteGroup *Result(uint16_t value)
	{
		REQUIRE(SpriteGroup::CanAllocateItem());
		auto *group = CallbackResultSpriteGroup::Create(value);
		groups.emplace_back(group);
		return group;
	}

	DeterministicSpriteGroup *Group(VarSpriteGroupScope scope)
	{
		REQUIRE(SpriteGroup::CanAllocateItem());
		auto *group = DeterministicSpriteGroup::Create();
		groups.emplace_back(group);
		group->var_scope = scope;
		group->size = DeterministicSpriteGroupSize::DWord;
		return group;
	}

	static DeterministicSpriteGroupAdjust Read(uint8_t variable, uint32_t parameter = 0)
	{
		DeterministicSpriteGroupAdjust adjust;
		adjust.variable = variable;
		adjust.parameter = parameter;
		adjust.and_mask = UINT32_MAX;
		adjust.operation = DeterministicSpriteGroupAdjustOperation::Rst;
		return adjust;
	}

	static DeterministicSpriteGroupAdjust Constant(uint32_t value, DeterministicSpriteGroupAdjustOperation operation = DeterministicSpriteGroupAdjustOperation::Rst)
	{
		auto adjust = Read(0x18);
		adjust.and_mask = 0;
		adjust.type = DeterministicSpriteGroupAdjustType::Div;
		adjust.divmod_val = 1;
		adjust.add_val = value;
		adjust.operation = operation;
		return adjust;
	}

	DeterministicSpriteGroup *TownGroup()
	{
		auto *group = Group(VarSpriteGroupScope::Parent);
		group->adjusts.push_back(Constant(FlattenNewGRFLabel(INVALID_GRFID)));
		group->adjusts.push_back(Constant(0x100, DeterministicSpriteGroupAdjustOperation::Sto));
		return group;
	}

	/** Name increments PSA[5], acceptance increments it again, AI denial reads it. */
	void Sequence(uint expected, uint16_t name_result = 0x400, bool malformed_deny = false)
	{
		auto *name = TownGroup();
		name->adjusts.push_back(Read(0x10));
		name->adjusts.push_back(Constant(7, DeterministicSpriteGroupAdjustOperation::Stop));
		name->adjusts.push_back(Read(0x7C, 5));
		name->adjusts.push_back(Constant(1, DeterministicSpriteGroupAdjustOperation::Add));
		name->adjusts.push_back(Constant(5, DeterministicSpriteGroupAdjustOperation::Stop));
		name->default_result.group = Result(name_result);
		auto *accept = TownGroup();
		accept->adjusts.push_back(Read(0x7C, 5));
		accept->adjusts.push_back(Constant(1, DeterministicSpriteGroupAdjustOperation::Add));
		accept->adjusts.push_back(Constant(5, DeterministicSpriteGroupAdjustOperation::Stop));
		accept->default_result.calculated_result = true;
		auto *deny = TownGroup();
		deny->adjusts.push_back(Read(0x7C, 5));
		deny->adjusts.push_back(Constant(6, DeterministicSpriteGroupAdjustOperation::Stop));
		deny->default_result.group = Result(malformed_deny ? 2 : 1);
		if (!malformed_deny) deny->ranges.push_back({{false, Result(0)}, expected, expected});
		auto *dispatch = Group(VarSpriteGroupScope::Self);
		dispatch->adjusts.push_back(Read(0x0C));
		dispatch->default_result.group = Result(CALLBACK_FAILED);
		dispatch->ranges.push_back({{false, accept}, CBID_HOUSE_CARGO_ACCEPTANCE, CBID_HOUSE_CARGO_ACCEPTANCE});
		dispatch->ranges.push_back({{false, deny}, CBID_HOUSE_DENY_DESTRUCTION, CBID_HOUSE_DENY_DESTRUCTION});
		dispatch->ranges.push_back({{false, name}, CBID_HOUSE_CUSTOM_NAME, CBID_HOUSE_CUSTOM_NAME});
		HouseSpec::Get(0)->grf_prop.SetSpriteGroup(StandardSpriteGroup::Default, dispatch);
		HouseSpec::Get(0)->callback_mask.Set(HouseCallbackMask::CargoAcceptance);
		HouseSpec::Get(0)->callback_mask.Set(HouseCallbackMask::DenyDestruction);
	}

	void House(bool complete = true)
	{
		MakeHouseTile(house_tile, town->index, 0, complete ? TOWN_HOUSE_COMPLETED : 0, 0, 37, false);
	}

	IndustryPresentationPsaOverlay Overlay() const { return {nullptr, nullptr, GrfID{}}; }

	Json Query(TileIndex tile) const
	{
		std::this_thread::sleep_for(std::chrono::milliseconds(1050));
		auto report = QueryReportsState("land.info", Json{{"tile", tile.base()}});
		REQUIRE(report.has_value());
		return *report;
	}
};

uint64_t MapFingerprint()
{
	uint64_t hash = 14695981039346656037ULL;
	for (Tile tile : Map::Iterate()) {
		for (uint16_t value : std::array<uint16_t, 10>{tile.type(), tile.height(), tile.m1(), tile.m2(), tile.m3(), tile.m4(), tile.m5(), tile.m6(), tile.m7(), tile.m8()}) {
			hash = (hash ^ (value & 255)) * 1099511628211ULL;
			hash = (hash ^ (value >> 8)) * 1099511628211ULL;
		}
	}
	return hash;
}

std::array<uint64_t, 14> PoolState()
{
	return {PersistentStorage::GetNumItems(), PersistentStorage::GetLastInstanceGeneration(),
		Company::GetNumItems(), Company::GetLastInstanceGeneration(), Town::GetNumItems(), Town::GetLastInstanceGeneration(),
		Industry::GetNumItems(), Industry::GetLastInstanceGeneration(), Station::GetNumItems(), Station::GetLastInstanceGeneration(),
		Depot::GetNumItems(), Depot::GetLastInstanceGeneration(),
		SpriteGroup::GetNumItems(), SpriteGroup::GetLastInstanceGeneration()};
}

struct Untouched {
	const LandInfoWorld &world;
	Randomizer game_random = _random, interactive_random = _interactive_random;
	uint64_t map = MapFingerprint();
	std::array<uint64_t, 14> pools = PoolState();
	Money money;
	uint32_t clear_limit;
	decltype(Town::ratings) ratings;
	decltype(Town::have_ratings) have_ratings;
	std::vector<PersistentStorage *> town_psa;
	std::vector<std::pair<const PersistentStorage *, std::array<int32_t, 256>>> storage;
	Window *error = FindWindowById(WindowClass::ErrorMessage, 0);
	GRFBugs bugs;

	explicit Untouched(const LandInfoWorld &world) : world(world), money(world.company->money), clear_limit(world.company->clear_limit),
		ratings(world.town->ratings), have_ratings(world.town->have_ratings), town_psa(world.town->psa_list), bugs(world.config->grf_bugs)
	{
		for (const PersistentStorage *psa : PersistentStorage::Iterate()) storage.emplace_back(psa, psa->storage);
	}

	void Check() const
	{
		REQUIRE(PoolState() == pools);
		CHECK(MapFingerprint() == map);
		CHECK(world.company->money == money);
		CHECK(world.company->clear_limit == clear_limit);
		CHECK(world.town->ratings == ratings);
		CHECK(world.town->have_ratings == have_ratings);
		CHECK(world.town->psa_list == town_psa);
		for (const auto &[psa, values] : storage) CHECK(psa->storage == values);
		for (uint i = 0; i < 2; ++i) {
			CHECK(_random.state[i] == game_random.state[i]);
			CHECK(_interactive_random.state[i] == interactive_random.state[i]);
		}
		CHECK(FindWindowById(WindowClass::ErrorMessage, 0) == error);
		CHECK(world.config->grf_bugs == bugs);
	}
};

} // namespace

TEST_CASE("Land information carries native house callbacks through one isolated query", "[integration][land-info]")
{
	const bool ai = GENERATE(false, true);
	const bool complete = GENERATE(false, true);
	const bool existing = GENERATE(false, true);
	LandInfoWorld world;
	world.company->is_ai = ai;
	const uint expected = existing ? 5 : 2;
	if (existing) {
		REQUIRE(PersistentStorage::CanAllocateItem());
		auto *psa = PersistentStorage::Create(world.grf.grfid, GrfSpecFeature::FakeTowns, world.town->xy);
		psa->storage[5] = 3;
		world.town->psa_list.push_back(psa);
	}
	world.Sequence(expected);
	world.House(complete);
	const Untouched before(world);
	auto scratch = world.Overlay();
	TileDesc desc{};
	GetHouseTileDescForObservation(world.house_tile, desc, scratch);
	CHECK(scratch.town_writes.at({world.town, world.grf.grfid})[7] == static_cast<int>(complete));
	CargoArray accepted{};
	CargoTypes always{};
	AddAcceptedCargoOfHouseForObservation(world.house_tile, accepted, always, scratch);
	CHECK(accepted[0] == expected);
	const auto clear = GetLandscapeClearCostForObservation(world.house_tile, scratch);
	CHECK(clear.Succeeded());
	CHECK(clear.GetCost() == HouseSpec::Get(0)->GetRemovalCost());
	const auto &writes = scratch.town_writes.at({world.town, world.grf.grfid});
	CHECK(writes[5] == expected);
	CHECK(writes[6].has_value() == ai);
	if (ai) CHECK(writes[6] == expected);
	const Json report = world.Query(world.house_tile);
	REQUIRE(report.at("accepted_cargo").size() == 1);
	CHECK(report.at("accepted_cargo").at(0).at("acceptance_eighths") == expected);
	CHECK(report.at("clear_preview").at("status") == "available");
	CHECK(report.at("clear_preview").at("cost") == clear.GetCost().base());
	CHECK(report.at("description") == GetString(desc.str, desc.dparam));
	CHECK_THROWS_AS(QueryReportsState("land.info", Json{{"tile", world.house_tile.base()}}), InvalidParameters);
	before.Check();
}

TEST_CASE("Land information suppresses malformed house callback diagnostics", "[integration][land-info]")
{
	LandInfoWorld world;
	world.grf.grf_version = GENERATE(7, 8);
	world.company->is_ai = true;
	world.Sequence(2, 0x401, true);
	world.House();
	world.config->grf_bugs.Set(GRFBug::UnknownCbResult);
	const Untouched before(world);
	const Json report = world.Query(world.house_tile);
	CHECK(report.at("description") == GetString(STR_CARGO_PLURAL_PASSENGERS));
	CHECK(report.at("clear_preview").at("status") == "blocked");
	CHECK(report.at("accepted_cargo").at(0).at("acceptance_eighths") == 2);
	before.Check();
}

TEST_CASE("Ordinary native house description retains its live callback storage path", "[integration][land-info]")
{
	LandInfoWorld world;
	world.Sequence(5);
	world.House();
	REQUIRE(PersistentStorage::CanAllocateItem());
	auto *psa = PersistentStorage::Create(world.grf.grfid, GrfSpecFeature::FakeTowns, world.town->xy);
	psa->storage[5] = 3;
	world.town->psa_list.push_back(psa);
	const auto count = PersistentStorage::GetNumItems();
	const auto generation = PersistentStorage::GetLastInstanceGeneration();
	TileDesc desc{};
	GetTileDesc(world.house_tile, desc);
	CHECK(desc.str == HouseSpec::Get(0)->building_name);
	CHECK(psa->GetValue(5) == 4);
	CHECK(psa->GetValue(7) == 1);
	CHECK(PersistentStorage::GetNumItems() == count);
	CHECK(PersistentStorage::GetLastInstanceGeneration() == generation);
}

TEST_CASE("Land information text rejection discards preceding native callback writes", "[integration][land-info]")
{
	LandInfoWorld world;
	world.Sequence(2, 0);
	world.House();
	AddGRFString(world.grf.grfid, GRFSTR_MISC_GRF_TEXT, static_cast<uint8_t>(GRFLanguage::English),
		true, true, std::string(1100, 'X'), STR_UNDEFINED);
	const Untouched before(world);
	auto scratch = world.Overlay();
	TileDesc desc{};
	GetHouseTileDescForObservation(world.house_tile, desc, scratch);
	CHECK(scratch.town_writes.at({world.town, world.grf.grfid})[5] == 1);
	CHECK(GetString(desc.str, desc.dparam).size() > 1024);
	CHECK_THROWS_AS(world.Query(world.house_tile), InvalidParameters);
	before.Check();
}

TEST_CASE("House observation clearing rejects execution flags before callbacks", "[integration][land-info]")
{
	LandInfoWorld world;
	world.company->is_ai = true;
	world.Sequence(2);
	world.House();
	const Untouched before(world);
	auto scratch = world.Overlay();
	CHECK(ClearHouseForObservation(world.house_tile, {}, scratch).Failed());
	CHECK(ClearHouseForObservation(world.house_tile, DoCommandFlag::Execute, scratch).Failed());
	CHECK(ClearHouseForObservation(world.house_tile, {DoCommandFlag::QueryCost, DoCommandFlag::Execute}, scratch).Failed());
	CHECK(GetLandscapeClearCostForObservation(INVALID_TILE, scratch).Failed());
	CHECK(GetLandscapeClearCostForObservation(TileIndex{Map::Size()}, scratch).Failed());
	CHECK(scratch.town_writes.empty());
	before.Check();
}

TEST_CASE("Land clear observation rejects non-company contexts before native work", "[integration][land-info]")
{
	LandInfoWorld world;
	const Untouched before(world);
	auto scratch = world.Overlay();
	for (CompanyID actor : {OWNER_WATER, OWNER_NONE, CompanyID::Invalid()}) {
		AutoRestoreBackup current(_current_company, actor);
		CHECK(GetLandscapeClearCostForObservation(world.house_tile, scratch).Failed());
	}
	CHECK(scratch.town_writes.empty());
	CHECK(scratch.industry_writes.empty());
	before.Check();
}

TEST_CASE("House observation retains player protection and callback property precedence", "[integration][land-info]")
{
	LandInfoWorld world;
	world.Sequence(2);
	world.House();
	auto scratch = world.Overlay();
	bool allowed = false;
	SECTION("AI respects player protection before invoking denial callbacks") {
		world.company->is_ai = true;
		SetHousePlayerProtected(world.house_tile, true);
	}
	SECTION("Human removal retains its native protection bypass") {
		world.company->is_ai = false;
		SetHousePlayerProtected(world.house_tile, true);
		allowed = true;
	}
	SECTION("AI callback may override the GRF protection property") {
		world.company->is_ai = true;
		HouseSpec::Get(0)->extra_flags.Set(HouseExtraFlag::BuildingIsProtected);
		TileDesc desc{};
		GetHouseTileDescForObservation(world.house_tile, desc, scratch);
		CargoArray accepted{};
		CargoTypes always{};
		AddAcceptedCargoOfHouseForObservation(world.house_tile, accepted, always, scratch);
		allowed = true;
	}
	const Untouched before(world);
	const auto cost = GetLandscapeClearCostForObservation(world.house_tile, scratch);
	CHECK(cost.Succeeded() == allowed);
	if (IsHousePlayerProtected(world.house_tile)) CHECK(scratch.town_writes.empty());
	before.Check();
}

TEST_CASE("Land information clear costs retain native rules limits and nested rating tests", "[integration][land-info]")
{
	const bool exhausted = GENERATE(false, true);
	const int kind = GENERATE(0, 1, 2);
	LandInfoWorld world;
	TileIndex tile = world.house_tile;
	if (kind == 1) world.House();
	if (kind == 2) {
		REQUIRE(Industry::CanAllocateItem());
		auto *industry = Industry::Create(tile);
		industry->type = 0;
		industry->town = world.town;
		_industry_specs[0].name = STR_CARGO_PLURAL_PASSENGERS;
		_industry_specs[0].grf_prop = {};
		_industry_tile_specs[0] = IndustryTileSpec{};
		_industry_tile_specs[0].grf_prop.override_id = INVALID_INDUSTRYTILE;
		_industry_tile_specs[0].accepts_cargo.fill(INVALID_CARGO);
		_industry_tile_specs[0].acceptance.fill(0);
		MakeIndustry(tile, industry->index, 0, 37, WaterClass::Invalid);
	}
	if (exhausted) world.company->clear_limit = 0;
	const Untouched before(world);
	const auto flags = CommandFlagsToDCFlags(GetCommandFlags<Commands::LandscapeClear>()) | DoCommandFlag::QueryCost;
	const auto native = Command<Commands::LandscapeClear>::Do(flags, tile);
	auto scratch = world.Overlay();
	const auto observed = GetLandscapeClearCostForObservation(tile, scratch);
	CHECK(observed.Succeeded() == native.Succeeded());
	CHECK(observed.GetErrorMessage() == native.GetErrorMessage());
	CHECK(observed.GetCost() == native.GetCost());
	if (exhausted) CHECK(observed.GetErrorMessage() == STR_ERROR_CLEARING_LIMIT_REACHED);
	if (kind == 1 && !exhausted) {
		AutoRestoreBackup<uint8_t> tolerance(_settings_game.difficulty.town_council_tolerance, TOWN_COUNCIL_TOLERANT);
		struct RatingScope {
			RatingScope() { SetTownRatingTestMode(true); }
			~RatingScope() { SetTownRatingTestMode(false); }
		} ratings;
		ChangeTownRating(world.town, RATING_ROAD_NEEDED_NEUTRAL + 5 - world.town->ratings[world.company->index], RATING_HOUSE_MINIMUM, {});
		CHECK(CheckforTownRating({}, world.town, TownRatingCheckType::RoadRemove).Succeeded());
		CHECK(GetLandscapeClearCostForObservation(tile, scratch).Succeeded());
		CHECK(CheckforTownRating({}, world.town, TownRatingCheckType::RoadRemove).Failed());
		CHECK(world.town->ratings[world.company->index] == 500);
	}
	const Json report = world.Query(tile);
	CHECK(report.at("clear_preview").at("status") == (native.Succeeded() ? "available" : "blocked"));
	if (native.Succeeded()) CHECK(report.at("clear_preview").at("cost") == native.GetCost().base());
	before.Check();
}

TEST_CASE("Land information keeps per-tile industry cargo beside neutral stations", "[integration][land-info]")
{
	LandInfoWorld world;
	AutoRestoreBackup serve_neutral(_settings_game.station.serve_neutral_industries, false);
	REQUIRE(Industry::CanAllocateItem());
	auto *industry = Industry::Create(world.house_tile);
	industry->type = 0;
	industry->town = world.town;
	REQUIRE(Station::CanAllocateItem());
	industry->neutral_station = Station::Create(TileXY(25, 25));
	_industry_specs[0].name = STR_CARGO_PLURAL_PASSENGERS;
	_industry_specs[0].grf_prop = {};
	auto &tile_spec = _industry_tile_specs[0];
	tile_spec = IndustryTileSpec{};
	tile_spec.grf_prop.override_id = INVALID_INDUSTRYTILE;
	tile_spec.accepts_cargo.fill(INVALID_CARGO);
	tile_spec.acceptance.fill(0);
	tile_spec.accepts_cargo[0] = static_cast<CargoType>(0);
	tile_spec.acceptance[0] = 8;
	MakeIndustry(world.house_tile, industry->index, 0, 37, WaterClass::Invalid);
	SetIndustryCompleted(world.house_tile);
	const Untouched before(world);
	auto scratch = world.Overlay();
	CHECK(GetAcceptanceAroundTiles(world.house_tile, 1, 1, 0, &scratch).first[0] == 0);
	const Json report = world.Query(world.house_tile);
	REQUIRE(report.at("accepted_cargo").size() == 1);
	CHECK(report.at("accepted_cargo").at(0).at("acceptance_eighths") == 8);
	CHECK(industry->psa == nullptr);
	before.Check();
}
