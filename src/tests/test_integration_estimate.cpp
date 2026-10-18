/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License, version 2.
 */

/** @file test_integration_estimate.cpp Native Shift feedback without gameplay submission. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions_internal.h"
#include "../integration/actions_story.h"
#include "../integration/visual_commands.h"
#include "../cargotype.h"
#include "../clear_map.h"
#include "../company_base.h"
#include "../core/endian_func.hpp"
#include "../core/pool_func.hpp"
#include "../core/random_func.hpp"
#include "../depot_base.h"
#include "../economy_func.h"
#include "../genworld.h"
#include "../engine_base.h"
#include "../engine_func.h"
#include "../house.h"
#include "../industry.h"
#include "../industrytype.h"
#include "../landscape.h"
#include "../landscape_cmd.h"
#include "../language.h"
#include "../network/network.h"
#include "../newgrf_config.h"
#include "../newgrf_house.h"
#include "../newgrf_spritegroup.h"
#include "../newgrf_storage.h"
#include "../road.h"
#include "../road_cmd.h"
#include "../road_map.h"
#include "../roadveh.h"
#include "../settings_type.h"
#include "../station_base.h"
#include "../strings_func.h"
#include "../town.h"
#include "../town_kdtree.h"
#include "../town_map.h"
#include "../townname_func.h"
#include "../vehicle_cmd.h"
#include "../window_func.h"
#include "../window_gui.h"

#include <chrono>
#include <fstream>
#include <thread>

#include "../table/cargo_const.h"

#include "../safeguards.h"

extern RoadTypeInfo _roadtypes[ROADTYPE_END];
extern RoadTypes _roadtypes_road;
extern RoadTypes _roadtypes_tram;

namespace {

using Integration::Json;
using Integration::Detail::ActionInvocation;
using Integration::Detail::Handle;

struct EstimateWorld {
	CompanyID previous_company = _current_company;
	CompanyID previous_local = _local_company;
	GameMode previous_mode = _game_mode;
	bool previous_networking = _networking;
	bool previous_server = _network_server;
	bool previous_generating = _generating_world;
	decltype(_settings_game.construction) previous_construction = _settings_game.construction;
	decltype(_settings_game.economy) previous_economy = _settings_game.economy;
	decltype(_settings_game.vehicle.max_roadveh) previous_max_roadveh = _settings_game.vehicle.max_roadveh;
	LandscapeType previous_landscape = _settings_game.game_creation.landscape;
	RoadTypes previous_roads = _roadtypes_road;
	RoadTypes previous_trams = _roadtypes_tram;
	RoadTypeInfo previous_road = _roadtypes[ROADTYPE_ROAD];
	Money previous_depot_price = _price[Price::BuildDepotRoad];
	Money previous_clear_price = _price[Price::ClearGrass];
	Money previous_remove_price = _price[Price::ClearDepotRoad];
	SavedRandomSeeds previous_seeds;
	Company *company;
	TileIndex tile;

	EstimateWorld()
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
		SaveRandomSeeds(&this->previous_seeds);
		_vehicle_pool.CleanPool();
		_engine_pool.CleanPool();
		_depot_pool.CleanPool();
		_station_pool.CleanPool();
		_industry_pool.CleanPool();
		_town_pool.CleanPool();
		_persistent_storage_pool.CleanPool();
		_company_pool.CleanPool();
		_town_kdtree.Clear();
		_town_local_authority_kdtree.Clear();
		Map::Allocate(64, 64);
		_settings_game.construction.freeform_edges = false;
		_settings_game.game_creation.landscape = LandscapeType::Temperate;
		_settings_game.vehicle.max_roadveh = 500;
		InitializeLandscape();
		_game_mode = GameMode::Normal;
		_generating_world = false;
		_networking = true;
		_network_server = true;
		_local_company = COMPANY_SPECTATOR;
		_roadtypes_road.Set(ROADTYPE_ROAD);
		_roadtypes_road.Reset(ROADTYPE_TRAM);
		_roadtypes_tram.Set(ROADTYPE_TRAM);
		_roadtypes_tram.Reset(ROADTYPE_ROAD);
		_roadtypes[ROADTYPE_ROAD].powered_roadtypes = {ROADTYPE_ROAD};
		_price[Price::BuildDepotRoad] = 123;
		_price[Price::ClearGrass] = 0;
		_price[Price::ClearDepotRoad] = 17;
		REQUIRE(Company::CanAllocateItem());
		this->company = Company::Create();
		_current_company = this->company->index;
		this->company->name = "Estimate fixture";
		this->company->money = 1000000;
		this->company->avail_roadtypes = {ROADTYPE_ROAD};
		this->company->clear_limit = UINT32_MAX;
		this->company->terraform_limit = UINT32_MAX;
		this->company->build_object_limit = UINT32_MAX;
		this->tile = TileXY(20, 20);
	}

	~EstimateWorld()
	{
		_vehicle_pool.CleanPool();
		_engine_pool.CleanPool();
		_depot_pool.CleanPool();
		_station_pool.CleanPool();
		_industry_pool.CleanPool();
		_town_pool.CleanPool();
		_persistent_storage_pool.CleanPool();
		_company_pool.CleanPool();
		_town_kdtree.Clear();
		_town_local_authority_kdtree.Clear();
		_current_company = this->previous_company;
		_local_company = this->previous_local;
		_game_mode = this->previous_mode;
		_networking = this->previous_networking;
		_network_server = this->previous_server;
		_generating_world = this->previous_generating;
		_settings_game.construction = this->previous_construction;
		_settings_game.economy = this->previous_economy;
		_settings_game.vehicle.max_roadveh = this->previous_max_roadveh;
		_settings_game.game_creation.landscape = this->previous_landscape;
		_roadtypes_road = this->previous_roads;
		_roadtypes_tram = this->previous_trams;
		_roadtypes[ROADTYPE_ROAD] = this->previous_road;
		_price[Price::BuildDepotRoad] = this->previous_depot_price;
		_price[Price::ClearGrass] = this->previous_clear_price;
		_price[Price::ClearDepotRoad] = this->previous_remove_price;
		RestoreRandomSeeds(this->previous_seeds);
	}

	Json Quote(const char *action, const Json &parameters) const
	{
		std::this_thread::sleep_for(std::chrono::milliseconds(1010));
		SavedRandomSeeds before;
		SaveRandomSeeds(&before);
		const auto pending = Integration::PendingLocalCommands();
		Json result = Integration::EstimateAction(action, parameters);
		CHECK(Integration::PendingLocalCommands() == pending);
		CHECK(_random.state[0] == before.random.state[0]);
		CHECK(_random.state[1] == before.random.state[1]);
		CHECK(_interactive_random.state[0] == before.interactive_random.state[0]);
		CHECK(_interactive_random.state[1] == before.interactive_random.state[1]);
		CHECK(_current_company == this->company->index);
		return result;
	}

	Json DepotParameters() const { return {{"tile", this->tile.base()}, {"direction", 0}}; }

	void Depot() const
	{
		REQUIRE(Depot::CanAllocateItem());
		auto *depot = Depot::Create(this->tile);
		MakeRoadDepot(this->tile, this->company->index, depot->index, DiagDirection::NE, ROADTYPE_ROAD);
	}
};

void CheckQuoteShape(const Json &result)
{
	REQUIRE(result.is_object());
	for (auto it = result.begin(); it != result.end(); ++it) {
		CHECK((it.key() == "success" || it.key() == "cost" || it.key() == "error" || it.key() == "advisory" || it.key() == "test_and_execute_may_differ"));
	}
	CHECK(result.at("success").is_boolean());
	CHECK(result.at("cost").is_number_integer());
	CHECK(result.at("advisory") == true);
	CHECK(result.at("test_and_execute_may_differ").is_boolean());
	CHECK(result.dump().size() <= 4096);
}

/** Native temporary vehicle destruction stops any main-viewport following. */
struct ScopedMainViewport {
	WindowDesc desc{WindowPosition::Manual, {}, 0, 0, WindowClass::MainWindow, WindowClass::None, WindowDefaultFlag::NoFocus, {}};
	Window *created = nullptr;

	ScopedMainViewport()
	{
		if (Window *existing = FindWindowById(WindowClass::MainWindow, 0); existing != nullptr) {
			REQUIRE(existing->viewport != nullptr);
			return;
		}
		this->created = new Window(this->desc);
		this->created->window_class = WindowClass::MainWindow;
		this->created->viewport = std::make_unique<ViewportData>();
		this->created->viewport->follow_vehicle = VehicleID::Invalid();
	}

	~ScopedMainViewport()
	{
		if (this->created == nullptr) return;
		this->created->Close();
		Window::DeleteClosedWindows();
	}
};

} // namespace

TEST_CASE("Action estimate quotes native build cost without funds or queue and recomputes after execution", "[integration][estimate]")
{
	EstimateWorld world;
	world.company->money = 0;
	const auto before_tile = GetTileType(world.tile);
	const Json quote = world.Quote("road.depot.build", world.DepotParameters());
	REQUIRE(quote.contains("estimate"));
	const Json result = quote.at("estimate");
	CheckQuoteShape(result);
	CHECK(result.at("success") == true);
	CHECK(result.at("cost") == 123);
	CHECK(result.at("test_and_execute_may_differ") == false);
	CHECK(GetTileType(world.tile) == before_tile);
	CHECK(Depot::GetNumItems() == 0);
	CHECK(world.company->money == 0);
	CHECK(world.company->infrastructure.road[ROADTYPE_ROAD] == 0);
	CHECK(Integration::EstimateAction("road.depot.build", world.DepotParameters()).at("error").at("code") == "estimate_rate_limited");

	/* The ordinary offline player Post executes synchronously and charges the
	 * real native cost. The estimate above ran under server networking instead. */
	_networking = false;
	world.company->money = 1000000;
	const Money money = world.company->money;
	REQUIRE(Integration::DispatchAction("road.depot.build", world.DepotParameters()).accepted);
	CHECK(money - world.company->money == Money(result.at("cost").get<int64_t>()));
	CHECK(IsRoadDepotTile(world.tile));
	CHECK(Depot::GetNumItems() == 1);
	_networking = true;
	const Json existing = world.Quote("road.depot.build", world.DepotParameters());
	REQUIRE(existing.contains("estimate"));
	CHECK(existing.at("estimate").at("cost") == 0);
	CHECK_FALSE(existing.at("estimate").contains("depot_id"));
	CheckQuoteShape(existing.at("estimate"));
	const Json removal = world.Quote("terrain.clear", {{"tile", world.tile.base()}});
	REQUIRE(removal.contains("estimate"));
	CHECK(removal.at("estimate").at("success") == true);
	CHECK(removal.at("estimate").at("cost") == 17);
	CHECK(IsRoadDepotTile(world.tile));
}

TEST_CASE("Action estimate refuses NoEst and UI input while exposing NoTest as advisory", "[integration][estimate]")
{
	EstimateWorld world;
	const bool enabled = world.company->settings.engine_renew;
	CHECK(world.Quote("company.setting.set", {{"key", "autorenew_enabled"}, {"value", !enabled}}).at("error").at("code") == "estimate_unavailable");
	CHECK(world.company->settings.engine_renew == enabled);
	CHECK(world.Quote("company.join_policy.set", {{"policy", "any"}}).at("error").at("code") == "estimate_unavailable");
	CHECK(Integration::EstimateAction("ui.apply", Json::object()).at("error").at("code") == "estimate_unavailable");
	const Json level = world.Quote("terrain.level", {{"start", world.tile.base()}, {"end", (world.tile + TileDiffXY(1, 0)).base()}});
	REQUIRE(level.contains("estimate"));
	CHECK(level.at("estimate").at("test_and_execute_may_differ") == true);
	CHECK(IsTileType(world.tile, TileType::Clear));
}

TEST_CASE("Estimate adapter preserves Location and ClientID types and drops tuple extras", "[integration][estimate]")
{
	EstimateWorld world;
	world.Depot();
	REQUIRE(RoadVehicle::CanAllocateItem());
	auto *vehicle = RoadVehicle::Create();
	vehicle->owner = world.company->index;
	vehicle->tile = world.tile;
	vehicle->state = RVSB_IN_DEPOT;
	vehicle->vehstatus = {VehState::Stopped, VehState::Hidden};
	vehicle->value = 71;
	const Json sale = world.Quote("vehicle.sell", {{"vehicle_id", Handle(vehicle)}});
	REQUIRE(sale.contains("estimate"));
	CheckQuoteShape(sale.at("estimate"));
	CHECK(sale.at("estimate").at("success") == true);
	CHECK(sale.at("estimate").at("cost") == -71);
	CHECK(RoadVehicle::GetNumItems() == 1);
	CHECK(vehicle->value == 71);
	CHECK(vehicle->vehstatus.Test(VehState::Stopped));

	/* No native first TileIndex: location defaults to zero, and an rvalue
	 * string must keep the native const-reference parameter conversion. */
	const Json rename = world.Quote("company.rename", {{"name", "After estimate"}});
	REQUIRE(rename.contains("estimate"));
	CHECK(rename.at("estimate").at("success") == true);
	CHECK(world.company->name == "Estimate fixture");
	/* Real tuple-return build/refit feedback cannot expose extra return values.
	 * Native ClientID::Invalid is normalized before the test is called. */
	struct RestoreCargo {
		CargoTypes mask = _cargo_mask;
		std::array<CargoSpec, 2> specs{*CargoSpec::Get(0), *CargoSpec::Get(1)};
		~RestoreCargo() { for (size_t i = 0; i < this->specs.size(); ++i) *CargoSpec::Get(i) = this->specs[i]; _cargo_mask = this->mask; }
	} restore_cargo;
	for (size_t i = 0; i < restore_cargo.specs.size(); ++i) {
		*CargoSpec::Get(i) = _default_cargo[i];
		_cargo_mask.Set(static_cast<CargoType>(i));
	}
	REQUIRE(Engine::CanAllocateItem());
	auto *engine = Engine::Create(VehicleType::Road, 0);
	engine->info.climates.Set(LandscapeType::Temperate);
	engine->info.cargo_type = static_cast<CargoType>(0);
	engine->info.refit_mask = {static_cast<CargoType>(0), static_cast<CargoType>(1)};
	engine->company_avail.Set(world.company->index);
	engine->VehInfo<RoadVehicleInfo>().roadtype = ROADTYPE_ROAD;
	engine->VehInfo<RoadVehicleInfo>().capacity = 15;
	REQUIRE(IsEngineBuildable(engine->index, VehicleType::Road, world.company->index));
	const Json build = world.Quote("vehicle.build", {{"tile", world.tile.base()}, {"engine_id", engine->index.base()}});
	INFO("Native build quote: " << build.dump());
	REQUIRE(build.contains("estimate"));
	CHECK(build.at("estimate").at("success") == true);
	CheckQuoteShape(build.at("estimate"));
	CHECK(RoadVehicle::GetNumItems() == 1);
	CHECK_FALSE(build.at("estimate").contains("vehicle_id"));
	CHECK_FALSE(build.at("estimate").contains("capacity"));
	const auto before_generation = Vehicle::GetLastInstanceGeneration();
	const auto before_money = world.company->money;
	ScopedMainViewport main_viewport;
	const Json refitted_build = world.Quote("vehicle.build", {{"tile", world.tile.base()}, {"engine_id", engine->index.base()}, {"cargo_id", 1}});
	INFO("Native refitted build quote: " << refitted_build.dump());
	REQUIRE(refitted_build.contains("estimate"));
	CHECK(refitted_build.at("estimate").at("success") == true);
	CheckQuoteShape(refitted_build.at("estimate"));
	CHECK(Vehicle::GetLastInstanceGeneration() > before_generation);
	CHECK(RoadVehicle::GetNumItems() == 1);
	CHECK(world.company->money == before_money);
	CHECK(vehicle->value == 71);
	CHECK(vehicle->vehstatus.Test(VehState::Stopped));
}

TEST_CASE("Action estimates restore name-generation RNG and reject stale handles before commands", "[integration][estimate]")
{
	EstimateWorld world;
	_settings_game.economy.found_town = TownFounding::Forbidden;
	Randomizer local = _interactive_random;
	uint32_t name_parts = 0;
	REQUIRE(GenerateTownName(local, &name_parts));
	CHECK((local.state[0] != _interactive_random.state[0] || local.state[1] != _interactive_random.state[1]));
	const Json town = world.Quote("town.found", {{"tile", world.tile.base()}});
	REQUIRE(town.contains("estimate"));
	CHECK(town.at("estimate").at("success") == false);
	CHECK(town.at("estimate").at("test_and_execute_may_differ") == true);
	CHECK(Town::GetNumItems() == 0);
	IndustrySpec *industry = &_industry_specs[0];
	struct RestoreIndustry {
		IndustrySpec *spec;
		IndustrySpec previous;
		~RestoreIndustry() { *this->spec = std::move(this->previous); }
	} restore_industry{industry, *industry};
	*industry = IndustrySpec{};
	industry->enabled = true;
	industry->layouts.emplace_back();
	industry->life_type = {IndustryLifeType::Extractive};
	_settings_game.construction.raw_industry_construction = 2;
	const Json prospect = world.Quote("industry.fund", {{"industry_type", 0}});
	REQUIRE(prospect.contains("estimate"));
	CHECK(prospect.at("estimate").at("success") == true);
	CHECK(Industry::GetNumItems() == 0);
	world.Depot();
	Json handle = Handle(Depot::GetByTile(world.tile));
	handle["generation"] = handle.at("generation").get<uint64_t>() + 1;
	CHECK(world.Quote("depot.rename", {{"depot_id", handle}, {"name", "Stale"}}).at("error").at("code") == "invalid_targets");
	CHECK(Depot::GetByTile(world.tile)->name.empty());
	CHECK(Integration::EstimateAction("road.depot.build", {{"tile", -1}}).at("error").at("code") == "invalid_parameters");
}

TEST_CASE("Estimate native test mode restores PSA values and town ratings while retaining allowed allocation", "[integration][estimate]")
{
	EstimateWorld world;
	const auto old_house_count = HouseSpec::Specs().size();
	if (old_house_count == 0) HouseSpec::Specs().resize(1);
	HouseSpec saved_house = *HouseSpec::Get(0);
	struct RestoreHouse {
		HouseSpec saved;
		size_t count;
		~RestoreHouse() { *HouseSpec::Get(0) = std::move(saved); HouseSpec::Specs().resize(count); }
	} restore{saved_house, old_house_count};
	GRFFile grf{};
	grf.grf_version = 8;
	grf.grfid = GrfID{"\x01\x02\x03\x04"};
	auto *spec = HouseSpec::Get(0);
	spec->enabled = true;
	spec->building_flags = {BuildingFlag::Size1x1};
	spec->remove_rating_decrease = 10;
	spec->callback_mask = {HouseCallbackMask::DenyDestruction};
	spec->grf_prop.grffile = &grf;
	spec->grf_prop.grfid = grf.grfid;
	REQUIRE(SpriteGroup::CanAllocateItem(2));
	std::unique_ptr<CallbackResultSpriteGroup> allowed(CallbackResultSpriteGroup::Create(0));
	std::unique_ptr<DeterministicSpriteGroup> writer(DeterministicSpriteGroup::Create());
	writer->var_scope = VarSpriteGroupScope::Parent;
	writer->size = DeterministicSpriteGroupSize::DWord;
	auto constant = [](uint32_t value, DeterministicSpriteGroupAdjustOperation op) {
		DeterministicSpriteGroupAdjust adjust;
		adjust.variable = 0x18;
		adjust.and_mask = 0;
		adjust.type = DeterministicSpriteGroupAdjustType::Div;
		adjust.divmod_val = 1;
		adjust.add_val = value;
		adjust.operation = op;
		return adjust;
	};
	writer->adjusts.push_back(constant(UINT32_MAX, DeterministicSpriteGroupAdjustOperation::Rst));
	writer->adjusts.push_back(constant(0x100, DeterministicSpriteGroupAdjustOperation::Sto));
	writer->adjusts.push_back(constant(99, DeterministicSpriteGroupAdjustOperation::Rst));
	writer->adjusts.push_back(constant(9, DeterministicSpriteGroupAdjustOperation::Stop));
	writer->default_result.group = allowed.get();
	spec->grf_prop.SetSpriteGroup(StandardSpriteGroup::Default, writer.get());
	REQUIRE(Town::CanAllocateItem());
	auto *town = Town::Create(world.tile);
	town->ratings[world.company->index] = 400;
	town->have_ratings.Set(world.company->index);
	MakeHouseTile(world.tile, town->index, 0, TOWN_HOUSE_COMPLETED, 0, 0, false);
	/* Native human companies bypass this protection callback. An ordinary AI
	 * company exercises the real allowed command callback path instead. */
	world.company->is_ai = true;
	const auto psa_count = PersistentStorage::GetNumItems();
	const auto psa_generation = PersistentStorage::GetLastInstanceGeneration();
	const Json first = world.Quote("terrain.clear", {{"tile", world.tile.base()}});
	REQUIRE(first.contains("estimate"));
	REQUIRE(first.at("estimate").at("success") == true);
	CHECK(IsTileType(world.tile, TileType::House));
	CHECK(town->ratings[world.company->index] == 400);
	REQUIRE(town->psa_list.size() == 1);
	CHECK(PersistentStorage::GetNumItems() == psa_count + 1);
	CHECK(PersistentStorage::GetLastInstanceGeneration() > psa_generation);
	CHECK(town->psa_list.front()->GetValue(9) == 0);
	town->psa_list.front()->storage[9] = 35;
	const auto generation_after_allocation = PersistentStorage::GetLastInstanceGeneration();
	const Json second = world.Quote("terrain.clear", {{"tile", world.tile.base()}});
	REQUIRE(second.contains("estimate"));
	CHECK(second.at("estimate").at("success") == true);
	CHECK(town->psa_list.front()->GetValue(9) == 35);
	CHECK(PersistentStorage::GetLastInstanceGeneration() == generation_after_allocation);
	CHECK(town->ratings[world.company->index] == 400);
	/* A subsequent native command scope must be persistent, proving test mode
	 * did not leak. This runs the same actual GRF callback rather than a mock. */
	BasePersistentStorageArray::SwitchMode(PSM_ENTER_COMMAND);
	CHECK(GetHouseCallback(CBID_HOUSE_DENY_DESTRUCTION, 0, 0, 0, town, world.tile) == 0);
	BasePersistentStorageArray::SwitchMode(PSM_LEAVE_COMMAND);
	CHECK(town->psa_list.front()->GetValue(9) == 99);
}
