/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License, version 2.
 */

/** @file test_integration_terraform.cpp Native terrain geometry and screenshot dependencies. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions.h"
#include "../integration/actions_internal.h"
#include "../clear_map.h"
#include "../command_func.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../core/pool_func.hpp"
#include "../core/random_func.hpp"
#include "../economy_func.h"
#include "../industry.h"
#include "../industry_cmd.h"
#include "../industry_map.h"
#include "../industrytype.h"
#include "../landscape.h"
#include "../misc/endian_buffer.hpp"
#include "../object_base.h"
#include "../object_map.h"
#include "../rail_map.h"
#include "../road_map.h"
#include "../settings_type.h"
#include "../terraform_cmd.h"
#include "../town.h"
#include "../town_kdtree.h"
#include "../tree_map.h"
#include "../void_map.h"

#include "table/strings.h"

#include <chrono>
#include <iostream>

#include "../safeguards.h"

extern RoadTypes _roadtypes_road;
extern RoadTypes _roadtypes_tram;

namespace {

struct TerrainWorld {
	CompanyID previous_company = _current_company;
	GameMode previous_mode = _game_mode;
	bool previous_freeform = _settings_game.construction.freeform_edges;
	uint8_t previous_height_limit = _settings_game.construction.map_height_limit;
	Money previous_terraform_price = _price[Price::Terraform];
	Money previous_clear_price = _price[Price::ClearGrass];
	RoadTypes previous_road_mask = GetMaskForRoadTramType(RoadTramType::Road);
	RoadTypes previous_tram_mask = GetMaskForRoadTramType(RoadTramType::Tram);
	SavedRandomSeeds previous_seeds;
	Company *company;
	TileIndex center;

	TerrainWorld()
	{
		SaveRandomSeeds(&this->previous_seeds);
		Map::Allocate(128, 128);
		_settings_game.construction.freeform_edges = false;
		_settings_game.construction.map_height_limit = 255;
		_game_mode = GameMode::Normal;
		_price[Price::Terraform] = 17;
		_price[Price::ClearGrass] = 0;
		/* A focused native test process has not run normal road-type startup.
		 * Establish the two builtin classifications without replacing GRF data. */
		_roadtypes_road.Set(ROADTYPE_ROAD);
		_roadtypes_road.Reset(ROADTYPE_TRAM);
		_roadtypes_tram.Set(ROADTYPE_TRAM);
		_roadtypes_tram.Reset(ROADTYPE_ROAD);
		REQUIRE(Company::CanAllocateItem());
		this->company = Company::Create();
		_current_company = this->company->index;
		this->center = TileXY(64, 64);
		this->Reset();
	}

	~TerrainWorld()
	{
		delete this->company;
		_current_company = this->previous_company;
		_game_mode = this->previous_mode;
		_settings_game.construction.freeform_edges = this->previous_freeform;
		_settings_game.construction.map_height_limit = this->previous_height_limit;
		_price[Price::Terraform] = this->previous_terraform_price;
		_price[Price::ClearGrass] = this->previous_clear_price;
		_roadtypes_road = this->previous_road_mask;
		_roadtypes_tram = this->previous_tram_mask;
		RestoreRandomSeeds(this->previous_seeds);
	}

	void Reset(uint radius = 0)
	{
		InitializeLandscape();
		this->company->money = 1'000'000'000;
		this->company->terraform_limit = UINT32_MAX;
		for (uint y = 0; y < Map::SizeY(); ++y) {
			for (uint x = 0; x < Map::SizeX(); ++x) {
				const uint distance = std::abs(static_cast<int>(x) - 64) + std::abs(static_cast<int>(y) - 64);
				SetTileHeight(TileXY(x, y), radius > distance ? radius - distance : 0);
			}
		}
	}

	std::vector<uint8_t> Heights() const
	{
		std::vector<uint8_t> result;
		result.reserve(Map::Size());
		for (uint index = 0; index < Map::Size(); ++index) result.push_back(TileHeight(TileIndex(index)));
		return result;
	}

	Integration::TargetWatermarks Watermarks() const
	{
		Integration::TargetWatermarks result;
		result.company = Company::GetLastInstanceGeneration();
		return result;
	}
};

template <Commands Tcommand, typename ... T>
CommandDataBuffer NativeData(T &&... values)
{
	using Args = typename CommandTraits<Tcommand>::Args;
	return EndianBufferWriter<CommandDataBuffer>::FromValue(Args{std::forward<T>(values)...});
}

/** A valid single-tile native industry with no callbacks or cargo side effects. */
struct PlatformIndustry {
	static constexpr IndustryType TYPE = 0;
	IndustrySpec previous = _industry_specs[TYPE];
	IndustryTileSpec previous_tile = _industry_tile_specs[0];
	uint8_t previous_policy = _settings_game.construction.raw_industry_construction;
	uint8_t previous_margin = _settings_game.construction.industry_platform;

	PlatformIndustry()
	{
		auto &spec = _industry_specs[TYPE];
		spec = IndustrySpec{};
		spec.layouts = {{{{0, 0}, 0}}};
		spec.enabled = true;
		spec.check_proc = IndustryCheck::None;
		spec.life_type = IndustryLifeTypes{IndustryLifeType::Processing};
		spec.produced_cargo.fill(INVALID_CARGO);
		spec.accepts_cargo.fill(INVALID_CARGO);
		std::ranges::fill(spec.conflicting, IT_INVALID);
		spec.cost_multiplier = 128;
		spec.name = STR_NULL;
		spec.new_industry_text = STR_NULL;
		_industry_tile_specs[0] = IndustryTileSpec{};
		_industry_tile_specs[0].enabled = true;
		_industry_tile_specs[0].slopes_refused = SLOPE_FLAT;
		_settings_game.construction.raw_industry_construction = 1;
		_settings_game.construction.industry_platform = 0;
	}

	~PlatformIndustry()
	{
		_industry_specs[TYPE] = std::move(this->previous);
		_industry_tile_specs[0] = std::move(this->previous_tile);
		_settings_game.construction.raw_industry_construction = this->previous_policy;
		_settings_game.construction.industry_platform = this->previous_margin;
	}

	CommandDataBuffer Data(TileIndex tile) const
	{
		return NativeData<Commands::BuildIndustry>(tile, TYPE, uint32_t{0}, true, uint32_t{1});
	}
};

} // namespace

TEST_CASE("Native terraform geometry admits a real cascade beyond 256 corners without writes", "[integration][terraform]")
{
	TerrainWorld world;
	world.Reset(20);
	const auto before = world.Heights();
	const auto quota = world.company->terraform_limit;
	const auto funds = world.company->money;
	TerraformGeometry geometry;
	const auto [geometry_cost, error_tile] = CollectTerraformLandGeometry(world.center, SLOPE_N, true, geometry);
	REQUIRE(geometry_cost.Succeeded());
	CHECK(error_tile == INVALID_TILE);
	/* A radius-20 Manhattan cone has 1 + 2*r*(r+1) corners. */
	REQUIRE(geometry.tile_to_new_height.size() == 841);
	CHECK(world.Heights() == before);
	CHECK(world.company->terraform_limit == quota);
	CHECK(world.company->money == funds);
	CHECK(geometry_cost.GetCost() == Money(841 * 17));

	const auto data = NativeData<Commands::TerraformLand>(world.center, SLOPE_N, true);
	const auto proof = Integration::CaptureNativeIntentProof(Commands::TerraformLand, data);
	CHECK(proof.tile_proof.at("count").get<size_t>() > 256);
	CHECK(Integration::VerifyNativeIntentProof(Commands::TerraformLand, data, world.Watermarks(), proof.tile_proof, proof.state).accepted);
	const auto [cost, extra_cash, result_tile] = Command<Commands::TerraformLand>::Do(DoCommandFlag::Execute, world.center, SLOPE_N, true);
	REQUIRE(cost.Succeeded());
	CHECK(cost.GetCost() == geometry_cost.GetCost());
	CHECK(extra_cash == 0);
	CHECK(result_tile == world.center);
	CHECK(world.company->terraform_limit == quota - (841U << 16));
	CHECK(world.company->money == funds - cost.GetCost());
	const auto after = world.Heights();
	for (uint index = 0; index < Map::Size(); ++index) {
		const auto changed = geometry.tile_to_new_height.find(TileIndex(index));
		const uint expected = changed == geometry.tile_to_new_height.end() ? before[index] : changed->second;
		REQUIRE(after[index] == expected);
	}
}

TEST_CASE("One-point native LevelLand preserves TerraformLand costs, heights and quota", "[integration][terraform]")
{
	TerrainWorld world;
	CHECK_THROWS(Integration::CaptureNativeIntentProof(Commands::LevelLand,
		NativeData<Commands::LevelLand>(world.center + TileDiffXY(1, 0), world.center, false, LevelMode::Raise)));
	CHECK_THROWS(Integration::CaptureNativeIntentProof(Commands::LevelLand,
		NativeData<Commands::LevelLand>(world.center, world.center, false, LevelMode::Level)));
	for (const auto mode : {LevelMode::Raise, LevelMode::Lower}) {
		const bool raise = mode == LevelMode::Raise;
		world.Reset(20);
		const auto [single_cost, single_extra, single_tile] = Command<Commands::TerraformLand>::Do(DoCommandFlag::Execute, world.center, SLOPE_N, raise);
		REQUIRE(single_cost.Succeeded());
		const auto heights = world.Heights();
		const auto quota = world.company->terraform_limit;
		const auto funds = world.company->money;

		world.Reset(20);
		const auto data = NativeData<Commands::LevelLand>(world.center, world.center, false, mode);
		const auto proof = Integration::CaptureNativeIntentProof(Commands::LevelLand, data);
		CHECK(Integration::VerifyNativeIntentProof(Commands::LevelLand, data, world.Watermarks(), proof.tile_proof, proof.state).accepted);
		const auto [level_cost, level_extra, level_tile] = Command<Commands::LevelLand>::Do(DoCommandFlag::Execute, world.center, world.center, false, mode);
		REQUIRE(level_cost.Succeeded());
		CHECK(level_cost.GetCost() == single_cost.GetCost());
		CHECK(level_extra == single_extra);
		CHECK(level_tile == single_tile);
		CHECK(world.Heights() == heights);
		CHECK(world.company->terraform_limit == quota);
		CHECK(world.company->money == funds);
	}
}

TEST_CASE("Terraform screenshot proof includes unchanged neighboring corner reads", "[integration][terraform]")
{
	TerrainWorld world;
	const auto data = NativeData<Commands::LevelLand>(world.center, world.center, false, LevelMode::Raise);
	const auto proof = Integration::CaptureNativeIntentProof(Commands::LevelLand, data);
	REQUIRE(Integration::VerifyNativeIntentProof(Commands::LevelLand, data, world.Watermarks(), proof.tile_proof, proof.state).accepted);
	TerraformGeometry geometry;
	REQUIRE(std::get<0>(CollectTerraformLandGeometry(world.center, SLOPE_N, true, geometry)).Succeeded());
	const TileIndex neighbor = world.center + TileDiffXY(1, 0);
	REQUIRE_FALSE(geometry.dirty_tiles.contains(neighbor));
	REQUIRE_FALSE(geometry.tile_to_new_height.contains(neighbor));
	SetTileHeight(neighbor, 1);
	CHECK_FALSE(Integration::VerifyNativeIntentProof(Commands::LevelLand, data, world.Watermarks(), proof.tile_proof, proof.state).accepted);
	const auto fresh = Integration::CaptureNativeIntentProof(Commands::LevelLand, data);
	CHECK(Integration::VerifyNativeIntentProof(Commands::LevelLand, data, world.Watermarks(), fresh.tile_proof, fresh.state).accepted);
}

TEST_CASE("Native terrain geometry preserves configured limits and freeform Void corners", "[integration][terraform]")
{
	TerrainWorld world;
	TerraformGeometry geometry;
	_settings_game.construction.map_height_limit = 0;
	const auto [too_high, too_high_tile] = CollectTerraformLandGeometry(world.center, SLOPE_N, true, geometry);
	const auto [native_too_high, native_extra, native_tile] = CmdTerraformLand({}, world.center, SLOPE_N, true);
	REQUIRE(too_high.Failed());
	CHECK(too_high.GetErrorMessage() == native_too_high.GetErrorMessage());
	CHECK(too_high_tile == native_tile);
	_settings_game.construction.map_height_limit = 255;
	const TileIndex edge = TileXY(1, 20);
	const auto [edge_cost, edge_error] = CollectTerraformLandGeometry(edge, SLOPE_N, true, geometry);
	const auto [native_edge, edge_extra, native_error] = CmdTerraformLand({}, edge, SLOPE_N, true);
	REQUIRE(edge_cost.Failed());
	CHECK(edge_cost.GetErrorMessage() == native_edge.GetErrorMessage());
	CHECK(edge_error == native_error);

	_settings_game.construction.freeform_edges = true;
	const TileIndex source = TileXY(Map::MaxX() - 1, 20);
	const TileIndex corner = TileXY(Map::MaxX(), 20);
	REQUIRE(IsTileType(corner, TileType::Void));
	const auto [void_cost, void_error] = CollectTerraformLandGeometry(source, SLOPE_W, true, geometry);
	REQUIRE(void_cost.Succeeded());
	CHECK(geometry.dirty_tiles.contains(corner));
	CHECK(geometry.tile_to_new_height.at(corner) == 1);
	CHECK(TileHeight(corner) == 0);
	const auto data = NativeData<Commands::TerraformLand>(source, SLOPE_W, true);
	const auto proof = Integration::CaptureNativeIntentProof(Commands::TerraformLand, data);
	CHECK(Integration::VerifyNativeIntentProof(Commands::TerraformLand, data, world.Watermarks(), proof.tile_proof, proof.state).accepted);
	const auto [native_void, void_extra, void_tile] = Command<Commands::TerraformLand>::Do(DoCommandFlag::Execute, source, SLOPE_W, true);
	REQUIRE(native_void.Succeeded());
	CHECK(native_void.GetCost() == void_cost.GetCost());
	CHECK(TileHeight(corner) == 1);
	CHECK(IsTileType(corner, TileType::Void));
}

TEST_CASE("Industry platform proof tracks geometry and infrastructure without natural animation", "[integration][terraform][industry-platform]")
{
	TerrainWorld world;
	PlatformIndustry industry;
	const auto data = industry.Data(world.center);
	auto capture = [&]() { return Integration::Detail::CaptureIndustryPlatformDependencies(data).value(); };
	const auto original = capture();
	const TileIndex distant = TileXY(100, 100);
	SetClearDensity(distant, 1);
	CHECK(capture().proof == original.proof);
	MakeTree(distant, TREE_TEMPERATE, 1, TreeGrowthStage::Growing1, TreeGround::Grass, 1);
	CHECK(capture().proof == original.proof);
	SetTreeGrowth(distant, TreeGrowthStage::Grown);
	CHECK(capture().proof == original.proof);
	MakeShore(distant);
	CHECK(capture().proof == original.proof);
	SetTileHeight(distant, 1);
	CHECK(capture().proof != original.proof);
	SetTileHeight(distant, 0);
	MakeClear(distant, ClearGround::Grass, 3);

	MakeRailNormal(distant, world.company->index, TrackBits{Track::Upper}, RAILTYPE_RAIL);
	const auto rail = capture();
	REQUIRE(rail.infrastructure_owners == std::vector<CompanyID>{world.company->index});
	SetSignalStates(distant, 15);
	SetTrackReservation(distant, TrackBits{Track::Upper});
	SetRailGroundType(distant, RailGroundType::Grass);
	CHECK(capture().proof == rail.proof);
	SetRailGroundType(distant, RailGroundType::HalfTileWater);
	CHECK(capture().proof != rail.proof);
	SetRailGroundType(distant, RailGroundType::Grass);
	MakeRailNormal(distant, world.company->index, TrackBits{Track::Lower}, RAILTYPE_RAIL);
	CHECK(capture().proof != rail.proof);

	MakeRoadNormal(distant, RoadBits{RoadBit::NE}, ROADTYPE_ROAD, INVALID_ROADTYPE, TownID::Invalid(), world.company->index, OWNER_NONE);
	const auto road = capture();
	CHECK(road.proof != original.proof);
	SetRoadBits(distant, ROAD_X, RoadTramType::Road);
	CHECK(capture().proof != road.proof);
	SetRoadBits(distant, RoadBits{RoadBit::NE}, RoadTramType::Road);
	SetRoadOwner(distant, RoadTramType::Road, OWNER_TOWN);
	CHECK(capture().proof != road.proof);
	CHECK(capture().infrastructure_owners.empty());

	_industry_specs[PlatformIndustry::TYPE].life_type = IndustryLifeTypes{IndustryLifeType::Extractive};
	_settings_game.construction.raw_industry_construction = 2;
	CHECK_FALSE(Integration::Detail::CaptureIndustryPlatformDependencies(data).has_value());
	const auto prospect = industry.Data(TileIndex(0));
	const auto no_selected_site = Integration::Detail::ConstructionIntentTiles(Commands::BuildIndustry, prospect);
	REQUIRE(no_selected_site.has_value());
	CHECK(no_selected_site->empty());
	CHECK(Integration::CaptureNativeIntentProof(Commands::BuildIndustry, prospect).tile_proof.at("count") == 0);
}

TEST_CASE("Industry platform validates only relevant company incarnations and native layout bounds", "[integration][terraform][industry-platform]")
{
	TerrainWorld world;
	PlatformIndustry industry;
	REQUIRE(Company::CanAllocateItem());
	std::unique_ptr<Company> owner(Company::Create());
	const TileIndex distant = TileXY(100, 100);
	MakeRoadNormal(distant, RoadBits{RoadBit::NE}, ROADTYPE_ROAD, INVALID_ROADTYPE, TownID::Invalid(), owner->index, OWNER_NONE);
	const auto data = industry.Data(world.center);
	const auto old = Integration::CaptureNativeIntentProof(Commands::BuildIndustry, data);
	const auto watermarks = world.Watermarks();
	REQUIRE(Integration::VerifyNativeIntentProof(Commands::BuildIndustry, data, watermarks, old.tile_proof, old.state).accepted);
	const auto id = owner->index;
	owner.reset();
	REQUIRE(Company::CanAllocateItem());
	owner.reset(Company::Create());
	REQUIRE(owner->index == id);
	/* Identical numeric owner and tile content cannot hide a new company. */
	CHECK_FALSE(Integration::VerifyNativeIntentProof(Commands::BuildIndustry, data, watermarks, old.tile_proof, old.state).accepted);
	CHECK(Integration::VerifyNativeIntentProof(Commands::BuildIndustry, data, world.Watermarks(), old.tile_proof, old.state).accepted);

	/* Native layout count and coordinate bounds replace the former 256 tile /
	 * 65536 total-entry limits; the collector stays a map-bounded bitmap. */
	IndustryTileLayout layout;
	for (int16_t y = 0; y < 17; ++y) for (int16_t x = 0; x < 17; ++x) layout.push_back({{x, y}, 0});
	_industry_specs[PlatformIndustry::TYPE].layouts.assign(255, layout);
	const auto footprint = Integration::Detail::ConstructionIntentTiles(Commands::BuildIndustry, data);
	REQUIRE(footprint.has_value());
	CHECK(footprint->size() > 256);
	CHECK(Integration::CaptureNativeIntentProof(Commands::BuildIndustry, data).tile_proof.at("count").get<size_t>() > 256);
}

TEST_CASE("Native funded industry keeps free platform leveling and protects pooled assets", "[integration][terraform][industry-platform]")
{
	TerrainWorld world;
	PlatformIndustry configuration;
	const Money previous_price = _price[Price::BuildIndustry];
	struct RestorePrice { Money value; ~RestorePrice() { _price[Price::BuildIndustry] = value; } } restore_price{previous_price};
	_price[Price::BuildIndustry] = 1000;
	REQUIRE(Town::CanAllocateItem());
	std::unique_ptr<Town> town(Town::Create(world.center));
	_town_kdtree.Insert(town->index);
	struct RemoveTownIndex { TownID id; ~RemoveTownIndex() { _town_kdtree.Remove(id); } } remove_town{town->index};
	const bool previous_multiple = _settings_game.economy.multiple_industry_per_town;
	struct RestoreMultiple { bool value; ~RestoreMultiple() { _settings_game.economy.multiple_industry_per_town = value; } } restore_multiple{previous_multiple};
	_settings_game.economy.multiple_industry_per_town = true;
	for (const bool protected_object : {false, true}) {
		world.Reset();
		/* A gentle uphill slope east of the site propagates beyond Expand(3). */
		for (uint y = 0; y < Map::SizeY(); ++y) for (uint x = 0; x < Map::SizeX(); ++x) SetTileHeight(TileXY(x, y), 5 + std::min<uint>(15, x > 64 ? x - 64 : 0));
		const TileIndex affected = TileXY(70, 64);
		std::unique_ptr<Object> object;
		if (!protected_object) {
			MakeRoadNormal(affected, RoadBits{RoadBit::NE}, ROADTYPE_ROAD, INVALID_ROADTYPE, TownID::Invalid(), world.company->index, OWNER_NONE);
			world.company->infrastructure.road[ROADTYPE_ROAD] = 1;
		}
		const auto data = configuration.Data(world.center);
		const auto proof = Integration::CaptureNativeIntentProof(Commands::BuildIndustry, data);
		if (protected_object) {
			/* A new protected blocker outside the selected site is handled by
			 * normal current-world native validation, never silently destroyed. */
			REQUIRE(Object::CanAllocateItem());
			object.reset(Object::Create(OBJECT_OWNED_LAND, nullptr, TileArea(affected, 1, 1), TimerGameCalendar::date, uint8_t{0}));
			MakeObject(affected, world.company->index, object->index, WaterClass::Invalid, 0);
		}
		REQUIRE(Integration::VerifyNativeIntentProof(Commands::BuildIndustry, data, world.Watermarks(), proof.tile_proof, proof.state).accepted);
		const auto quota = world.company->terraform_limit;
		const auto funds = world.company->money;
		REQUIRE(Industry::CanAllocateItem());
		const auto cost = Command<Commands::BuildIndustry>::Do(DoCommandFlag::Execute, world.center, PlatformIndustry::TYPE, uint32_t{0}, true, uint32_t{1});
		REQUIRE(cost.Succeeded());
		REQUIRE(IsTileType(world.center, TileType::Industry));
		std::unique_ptr<Industry> created(Industry::GetByTile(world.center));
		CHECK(cost.GetCost() == 500);
		CHECK(world.company->money == funds - 500);
		CHECK(world.company->terraform_limit == quota);
		if (protected_object) {
			CHECK(IsTileType(affected, TileType::Object));
			CHECK(Object::GetByTile(affected) == object.get());
			CHECK(TileHeight(affected) == 11);
		} else {
			CHECK_FALSE(IsNormalRoadTile(affected));
			CHECK(world.company->infrastructure.road[ROADTYPE_ROAD] == 0);
			CHECK(TileHeight(affected) == 10);
		}
		created.reset();
		object.reset();
	}
}

TEST_CASE("Industry platform proof streams the maximum native map", "[.][integration][industry-platform-benchmark]")
{
	TerrainWorld world;
	PlatformIndustry industry;
	Map::Allocate(MAX_MAP_SIZE, MAX_MAP_SIZE);
	InitializeLandscape();
	const auto data = industry.Data(TileXY(64, 64));
	const auto start = std::chrono::steady_clock::now();
	const auto before = Integration::Detail::CaptureIndustryPlatformDependencies(data);
	const auto elapsed = std::chrono::steady_clock::now() - start;
	REQUIRE(before.has_value());
	CHECK(before->proof.at("digest").get<std::string>().size() == 64);
	CHECK(before->infrastructure_owners.empty());
	SetClearDensity(TileXY(Map::SizeX() - 10, Map::SizeY() - 10), 1);
	CHECK(Integration::Detail::CaptureIndustryPlatformDependencies(data)->proof == before->proof);
	std::cout << "industry platform proof, " << Map::Size() << " tiles: "
		<< std::chrono::duration_cast<std::chrono::milliseconds>(elapsed).count() << " ms\n";
}
