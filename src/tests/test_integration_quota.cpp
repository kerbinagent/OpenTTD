/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License, version 2.
 */

/** @file test_integration_quota.cpp Compact proof and native fleet/geometry boundary regressions. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions.h"
#include "../integration/actions_spatial.h"
#include "../integration/actions_vehicle_scope.h"
#include "../group_cmd.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../clear_map.h"
#include "../core/pool_func.hpp"
#include "../core/random_func.hpp"
#include "../depot_base.h"
#include "../house.h"
#include "../misc/endian_buffer.hpp"
#include "../newgrf_object.h"
#include "../newgrf_station.h"
#include "../object_base.h"
#include "../object_cmd.h"
#include "../object_map.h"
#include "../openttd.h"
#include "../roadveh.h"
#include "../road_cmd.h"
#include "../road_map.h"
#include "../station_cmd.h"
#include "../settings_type.h"
#include "../town.h"
#include "../town_cmd.h"
#include "../train.h"
#include "../train_cmd.h"
#include "../rail_map.h"
#include "../tunnelbridge_cmd.h"
#include "../vehicle_cmd.h"
#include "../vehicle_func.h"

#include "../safeguards.h"

namespace {

struct QuotaWorld {
	CompanyID previous_company = _current_company;
	Company *company;
	QuotaWorld()
	{
		_vehicle_pool.CleanPool();
		_orderlist_pool.CleanPool();
		_station_pool.CleanPool();
		_company_pool.CleanPool();
		Map::Allocate(512, 512);
		StationClass::Reset();
		REQUIRE(Company::CanAllocateItem());
		this->company = Company::Create();
		_current_company = this->company->index;
	}
	~QuotaWorld()
	{
		_vehicle_pool.CleanPool();
		_orderlist_pool.CleanPool();
		_station_pool.CleanPool();
		_company_pool.CleanPool();
		_current_company = this->previous_company;
	}
	Integration::TargetWatermarks Watermarks() const
	{
		return {Vehicle::GetLastInstanceGeneration(), BaseStation::GetLastInstanceGeneration(), 0, Company::GetLastInstanceGeneration()};
	}
};

template <Commands Tcommand, typename ... T>
CommandDataBuffer Data(T &&... values)
{
	using Args = CommandTraits<Tcommand>::Args;
	return EndianBufferWriter<CommandDataBuffer>::FromValue(Args{std::forward<T>(values)...});
}

} // namespace

TEST_CASE("Spatial proof canonically covers64-sized station joins and last-tile changes", "[integration][quota]")
{
	QuotaWorld world;
	const auto command = Data<Commands::BuildRailStation>(TileXY(32, 32), RAILTYPE_RAIL, Axis::Y, uint8_t{64}, uint8_t{64}, STAT_CLASS_DFLT, uint16_t{0}, NEW_STATION, false);
	const auto proof = Integration::CaptureNativeIntentProof(Commands::BuildRailStation, command);
	CHECK(proof.tile_proof.at("count") == 4356);
	CHECK(proof.tile_proof.dump().size() < 100);
	CHECK(proof.state.dump().size() <= 16384);
	CHECK(Integration::VerifyNativeIntentProof(Commands::BuildRailStation, command, world.Watermarks(), proof.tile_proof, proof.state).accepted);
	Tile(TileXY(96, 96)).m1() ^= 1;
	CHECK_FALSE(Integration::VerifyNativeIntentProof(Commands::BuildRailStation, command, world.Watermarks(), proof.tile_proof, proof.state).accepted);
	Tile(TileXY(96, 96)).m1() ^= 1;
	CHECK(Integration::VerifyNativeIntentProof(Commands::BuildRailStation, command, world.Watermarks(), proof.tile_proof, proof.state).accepted);
	auto malformed = proof.tile_proof;
	malformed["count"] = uint32_t{4355};
	CHECK_FALSE(Integration::VerifyNativeIntentProof(Commands::BuildRailStation, command, world.Watermarks(), malformed, proof.state).accepted);
	malformed = proof.tile_proof;
	malformed["digest"] = std::string(64, 'A');
	CHECK_FALSE(Integration::VerifyNativeIntentProof(Commands::BuildRailStation, command, world.Watermarks(), malformed, proof.state).accepted);
}

TEST_CASE("Compact spatial proofs cover a long native span without per-tile wire records", "[integration][quota]")
{
	QuotaWorld world;
	const auto command = Data<Commands::BuildBridge>(TileXY(450, 20), TileXY(10, 20), TransportType::Rail, BridgeType{0}, RAILTYPE_RAIL, INVALID_ROADTYPE);
	const auto proof = Integration::CaptureNativeIntentProof(Commands::BuildBridge, command);
	CHECK(proof.tile_proof.at("count") == 441);
	CHECK(proof.tile_proof.dump().size() < 100);
	CHECK(Integration::VerifyNativeIntentProof(Commands::BuildBridge, command, world.Watermarks(), proof.tile_proof, proof.state).accepted);
}

TEST_CASE("Shared membership permits10000 primary heads and rejects overflow", "[integration][quota]")
{
	QuotaWorld world;
	REQUIRE(Vehicle::CanAllocateItem());
	auto *first = RoadVehicle::Create();
	first->owner = _current_company;
	first->SetFrontEngine();
	for (uint i = 1; i < 10000; ++i) {
		REQUIRE(Vehicle::CanAllocateItem());
		auto *vehicle = RoadVehicle::Create();
		vehicle->owner = _current_company;
		vehicle->SetFrontEngine();
		if (first->orders == nullptr) REQUIRE(OrderList::CanAllocateItem());
		vehicle->orders = first->orders;
		vehicle->AddToShared(first);
	}
	CHECK(Integration::Detail::NativeSharedVehicles(first).size() == 10000);
	REQUIRE(Vehicle::CanAllocateItem());
	auto *overflow = RoadVehicle::Create();
	overflow->owner = _current_company;
	overflow->SetFrontEngine();
	overflow->orders = first->orders;
	overflow->AddToShared(first);
	CHECK_THROWS_AS(Integration::Detail::NativeSharedVehicles(first), Integration::Detail::InvalidParameters);
}

TEST_CASE("Consist proof streams1024 exact parts and fences a changed final part", "[integration][quota]")
{
	QuotaWorld world;
	REQUIRE(Vehicle::CanAllocateItem());
	auto *first = Train::Create();
	first->owner = _current_company;
	first->SetFrontEngine();
	Train *last = first;
	for (uint i = 1; i < 1024; ++i) {
		REQUIRE(Vehicle::CanAllocateItem());
		auto *part = Train::Create();
		part->owner = _current_company;
		last->SetNext(part);
		last = part;
	}
	const auto command = Data<Commands::SellVehicle>(first->index, true, false, ClientID::Invalid);
	const auto proof = Integration::CaptureNativeIntentProof(Commands::SellVehicle, command);
	CHECK(proof.state.at("consists").at("part_count") == 1024);
	CHECK(proof.state.dump().size() < 1024);
	CHECK(Integration::VerifyNativeIntentProof(Commands::SellVehicle, command, world.Watermarks(), proof.tile_proof, proof.state).accepted);
	++last->cargo_cap;
	CHECK_FALSE(Integration::VerifyNativeIntentProof(Commands::SellVehicle, command, world.Watermarks(), proof.tile_proof, proof.state).accepted);
}

TEST_CASE("Shared group moves fence peer parts only when include_shared is requested", "[integration][quota]")
{
	QuotaWorld world;
	REQUIRE(Vehicle::CanAllocateItem());
	auto *first = Train::Create();
	first->owner = _current_company;
	first->SetFrontEngine();
	REQUIRE(Vehicle::CanAllocateItem());
	auto *peer = Train::Create();
	peer->owner = _current_company;
	peer->SetFrontEngine();
	REQUIRE(OrderList::CanAllocateItem());
	peer->orders = first->orders;
	peer->AddToShared(first);
	const auto selected = Data<Commands::AddVehicleToGroup>(DEFAULT_GROUP, first->index, false, VehicleListIdentifier{});
	const auto shared = Data<Commands::AddVehicleToGroup>(DEFAULT_GROUP, first->index, true, VehicleListIdentifier{});
	const auto selected_proof = Integration::CaptureNativeIntentProof(Commands::AddVehicleToGroup, selected);
	const auto shared_proof = Integration::CaptureNativeIntentProof(Commands::AddVehicleToGroup, shared);
	const auto source = world.Watermarks();
	REQUIRE(Vehicle::CanAllocateItem());
	auto *new_part = Train::Create();
	new_part->owner = _current_company;
	peer->SetNext(new_part);
	REQUIRE(new_part->GetInstanceGeneration() > source.vehicle);
	CHECK(Integration::VerifyNativeIntentProof(Commands::AddVehicleToGroup, selected, source, selected_proof.tile_proof, selected_proof.state).accepted);
	CHECK_FALSE(Integration::VerifyNativeIntentProof(Commands::AddVehicleToGroup, shared, source, shared_proof.tile_proof, shared_proof.state).accepted);
}

TEST_CASE("Ordinary road-piece capture accepts the native unused town sentinel", "[integration][quota]")
{
	QuotaWorld world;
	/* CmdBuildRoad requires Invalid for an ordinary company; it derives any
	 * nearby town itself. Native depot-approach GUI posts use this sentinel. */
	const auto command = Data<Commands::BuildRoad>(TileXY(32, 32), ROAD_X, ROADTYPE_ROAD, DisallowedRoadDirections{}, TownID::Invalid());
	const auto proof = Integration::CaptureNativeIntentProof(Commands::BuildRoad, command);
	CHECK(proof.tile_proof.at("count") == 1);
	CHECK(Integration::VerifyNativeIntentProof(Commands::BuildRoad, command, world.Watermarks(), proof.tile_proof, proof.state).accepted);
}

namespace {

struct WagonMoveWorld : QuotaWorld {
	WagonMoveWorld()
	{
		ResetVehicleHash();
		_depot_pool.CleanPool();
	}
	~WagonMoveWorld()
	{
		ResetVehicleHash();
		_depot_pool.CleanPool();
	}

	Depot *MakeDepot(TileIndex tile)
	{
		REQUIRE(Depot::CanAllocateItem());
		auto *depot = Depot::Create(tile);
		MakeRailDepot(tile, _current_company, depot->index, DiagDirection::NE, RAILTYPE_RAIL);
		return depot;
	}

	Train *Part(TileIndex tile, EngineID engine, bool free_wagon = false, bool front_engine = false)
	{
		REQUIRE(Vehicle::CanAllocateItem());
		auto *part = Train::Create();
		part->owner = _current_company;
		part->engine_type = engine;
		part->tile = tile;
		part->track = Track::Depot;
		part->vehstatus.Set({VehState::Hidden, VehState::Stopped});
		if (front_engine) { part->SetEngine(); part->SetFrontEngine(); }
		else part->SetWagon();
		if (free_wagon) part->SetFreeWagon();
		part->UpdatePosition();
		return part;
	}

	Train *Source(TileIndex tile)
	{
		auto *head = this->Part(tile, EngineID{0}, false, true);
		auto *source = this->Part(tile, EngineID{1});
		head->SetNext(source);
		return source;
	}

	Integration::TargetWatermarks Watermarks() const
	{
		auto result = QuotaWorld::Watermarks();
		result.depot = Depot::GetLastInstanceGeneration();
		return result;
	}
};

} // namespace

TEST_CASE("Implicit wagon selector preserves native sorted matching and self-selection", "[integration][quota][wagon]")
{
	WagonMoveWorld world;
	const auto tile = TileXY(32, 32);
	world.MakeDepot(tile);
	auto *source = world.Source(tile);
	auto *mixed = world.Part(tile, EngineID{1}, true);
	auto *different_part = world.Part(tile, EngineID{2});
	mixed->SetNext(different_part);
	auto *matching = world.Part(tile, EngineID{1}, true);
	CHECK(FindRailVehicleMoveDestination(source) == matching->index);
	CHECK(FindRailVehicleMoveDestination(matching) == matching->index);
	different_part->engine_type = EngineID{1};
	CHECK(FindRailVehicleMoveDestination(source) == mixed->index);
	mixed->vehstatus.Set(VehState::Crashed);
	CHECK(FindRailVehicleMoveDestination(source) == matching->index);
}

TEST_CASE("Implicit wagon proofs track selected destinations and permit irrelevant arrivals", "[integration][quota][wagon]")
{
	WagonMoveWorld world;
	const auto tile = TileXY(32, 32), other = TileXY(40, 32);
	world.MakeDepot(tile);
	world.MakeDepot(other);
	auto *source = world.Source(tile);
	auto *old_candidate = world.Part(other, EngineID{1}, true);
	const auto command = Data<Commands::MoveRailVehicle>(source->index, VehicleID::Invalid(), false);
	const Integration::Json parameters = {{"vehicle_id", Integration::Detail::Handle(source)}};
	const auto proof = Integration::CaptureNativeIntentProof(Commands::MoveRailVehicle, command);
	const auto typed = Integration::CaptureActionTargets("train.wagon.move", parameters);
	REQUIRE(typed.is_array());
	const auto frame = world.Watermarks();
	CHECK(proof.state.at("implicit_wagon_move").at("selected_vehicle_id").is_null());
	world.Part(tile, EngineID{2}, true); // New, but irrelevant to native selection.
	CHECK(Integration::VerifyNativeIntentProof(Commands::MoveRailVehicle, command, frame, proof.tile_proof, proof.state).accepted);
	CHECK(Integration::ValidateActionTargets("train.wagon.move", parameters, typed).accepted);
	old_candidate->tile = tile;
	old_candidate->UpdatePosition();
	REQUIRE(old_candidate->GetInstanceGeneration() <= frame.vehicle);
	CHECK_FALSE(Integration::VerifyNativeIntentProof(Commands::MoveRailVehicle, command, frame, proof.tile_proof, proof.state).accepted);
	CHECK_FALSE(Integration::ValidateActionTargets("train.wagon.move", parameters, typed).accepted);
}

TEST_CASE("Implicit wagon proofs fence selected parts and the source depot", "[integration][quota][wagon]")
{
	WagonMoveWorld world;
	const auto tile = TileXY(32, 32);
	auto *depot = world.MakeDepot(tile);
	auto *source = world.Source(tile);
	auto *selected = world.Part(tile, EngineID{1}, true);
	const auto command = Data<Commands::MoveRailVehicle>(source->index, VehicleID::Invalid(), false);
	const Integration::Json parameters = {{"vehicle_id", Integration::Detail::Handle(source)}};
	const auto proof = Integration::CaptureNativeIntentProof(Commands::MoveRailVehicle, command);
	const auto typed = Integration::CaptureActionTargets("train.wagon.move", parameters);
	REQUIRE(typed.is_array());
	const auto frame = world.Watermarks();
	REQUIRE(proof.state.at("implicit_wagon_move").at("selected_vehicle_id") == selected->index.base());
	CHECK(Integration::VerifyNativeIntentProof(Commands::MoveRailVehicle, command, frame, proof.tile_proof, proof.state).accepted);
	SECTION("a later matching candidate does not change the selected destination") {
		auto *later = world.Part(tile, EngineID{1}, true);
		REQUIRE(later->index > selected->index);
		REQUIRE(later->GetInstanceGeneration() > frame.vehicle);
		CHECK(FindRailVehicleMoveDestination(source) == selected->index);
		CHECK(Integration::VerifyNativeIntentProof(Commands::MoveRailVehicle, command, frame, proof.tile_proof, proof.state).accepted);
		CHECK(Integration::ValidateActionTargets("train.wagon.move", parameters, typed).accepted);
	}
	SECTION("selected chain gains a post-frame part") {
		auto *new_part = world.Part(tile, EngineID{1});
		selected->SetNext(new_part);
		REQUIRE(new_part->GetInstanceGeneration() > frame.vehicle);
		CHECK_FALSE(Integration::VerifyNativeIntentProof(Commands::MoveRailVehicle, command, frame, proof.tile_proof, proof.state).accepted);
		CHECK_FALSE(Integration::ValidateActionTargets("train.wagon.move", parameters, typed).accepted);
	}
	SECTION("source depot slot is reused") {
		const auto old_id = depot->index;
		delete depot;
		auto *replacement = world.MakeDepot(tile);
		REQUIRE(replacement->index == old_id);
		REQUIRE(replacement->GetInstanceGeneration() > frame.depot);
		CHECK_FALSE(Integration::VerifyNativeIntentProof(Commands::MoveRailVehicle, command, frame, proof.tile_proof, proof.state).accepted);
		CHECK_FALSE(Integration::ValidateActionTargets("train.wagon.move", parameters, typed).accepted);
	}
	SECTION("engine bypass remains null despite a matching wagon") {
		source->SetEngine();
		const auto engine_proof = Integration::CaptureNativeIntentProof(Commands::MoveRailVehicle, command);
		CHECK(engine_proof.state.at("implicit_wagon_move").at("selected_vehicle_id").is_null());
		CHECK_FALSE(Integration::VerifyNativeIntentProof(Commands::MoveRailVehicle, command, frame, proof.tile_proof, proof.state).accepted);
	}
}

TEST_CASE("Spatial construction proof ignores only the invisible Clear growth counter", "[integration][quota][spatial]")
{
	QuotaWorld world;
	const TileIndex tile = TileXY(32, 32);
	MakeClear(tile, ClearGround::Grass, 0);
	const auto command = Data<Commands::BuildRoad>(tile, ROAD_X, ROADTYPE_ROAD, DisallowedRoadDirections{}, TownID::Invalid());
	const auto proof = Integration::CaptureNativeIntentProof(Commands::BuildRoad, command);
	for (uint counter = 0; counter < 8; ++counter) {
		SetClearCounter(tile, counter);
		CHECK(Integration::VerifyNativeIntentProof(Commands::BuildRoad, command, world.Watermarks(), proof.tile_proof, proof.state).accepted);
	}
	SECTION("density can change native clearing cost") { SetClearDensity(tile, 1); }
	SECTION("ground kind changes native clearing cost") { SetClearGroundDensity(tile, ClearGround::Rocks, 0); }
	SECTION("height changes construction geometry") { SetTileHeight(tile, 1); }
	SECTION("ownership remains committed") { SetTileOwner(tile, _current_company); }
	SECTION("new infrastructure remains committed") { MakeRoadNormal(tile, ROAD_X, ROADTYPE_ROAD, INVALID_ROADTYPE, TownID::Invalid(), _current_company, OWNER_NONE); }
	CHECK_FALSE(Integration::VerifyNativeIntentProof(Commands::BuildRoad, command, world.Watermarks(), proof.tile_proof, proof.state).accepted);
}

TEST_CASE("Spatial proof retains upper m5 bits on every non-Clear tile", "[integration][quota][spatial]")
{
	QuotaWorld world;
	const TileIndex tile = TileXY(32, 32);
	MakeRoadNormal(tile, ROAD_X, ROADTYPE_ROAD, INVALID_ROADTYPE, TownID::Invalid(), _current_company, OWNER_NONE);
	const auto command = Data<Commands::BuildRoad>(tile, ROAD_Y, ROADTYPE_ROAD, DisallowedRoadDirections{}, TownID::Invalid());
	Integration::Detail::NativeTileSet footprint;
	footprint.insert(tile);
	const auto proof = Integration::Detail::NativeSpatialProof(Commands::BuildRoad, command, footprint);
	for (uint bit = 5; bit < 8; ++bit) {
		Tile(tile).m5() ^= uint8_t{1} << bit;
		CHECK(Integration::Detail::NativeSpatialProof(Commands::BuildRoad, command, footprint) != proof);
		Tile(tile).m5() ^= uint8_t{1} << bit;
	}
	CHECK(Integration::Detail::NativeSpatialProof(Commands::BuildRoad, command, footprint) == proof);
}

TEST_CASE("Native picker area commands admit only genuine one-point construction", "[integration][quota][point-picker]")
{
	QuotaWorld world;
	ResetObjects();
	const TileIndex tile = TileXY(32, 32);
	MakeClear(tile, ClearGround::Grass, 3);
	const auto object = Data<Commands::BuildObjectArea>(tile, tile, ObjectType{OBJECT_OWNED_LAND}, uint8_t{0}, false);
	const auto point = Data<Commands::BuildObject>(tile, ObjectType{OBJECT_OWNED_LAND}, uint8_t{0});
	REQUIRE(Integration::IsNativeCommandForAction("object.build", Commands::BuildObjectArea));
	REQUIRE_FALSE(Integration::IsNativeCommandForAction("house.build", Commands::BuildObjectArea));
	const auto before = Integration::CaptureNativeIntentProof(Commands::BuildObjectArea, object);
	const auto point_proof = Integration::CaptureNativeIntentProof(Commands::BuildObject, point);
	CHECK(before.tile_proof.at("count") == point_proof.tile_proof.at("count"));
	/* The same dependency geometry still binds the exact original wire command. */
	CHECK(before.tile_proof != point_proof.tile_proof);
	CHECK(before.state == Integration::NativeIntentState(Commands::BuildObject, point));
	CHECK(Integration::VerifyNativeIntentProof(Commands::BuildObjectArea, object, world.Watermarks(), before.tile_proof, before.state).accepted);
	CHECK(Integration::VerifyNativeIntentProof(Commands::BuildObject, point, world.Watermarks(), point_proof.tile_proof, point_proof.state).accepted);
	CHECK_THROWS(Integration::CaptureNativeIntentProof(Commands::BuildObjectArea,
		Data<Commands::BuildObjectArea>(tile, tile + TileDiffXY(1, 0), ObjectType{OBJECT_OWNED_LAND}, uint8_t{0}, false)));
	CHECK_THROWS(Integration::CaptureNativeIntentProof(Commands::BuildObjectArea,
		Data<Commands::BuildObjectArea>(tile, tile, ObjectType{OBJECT_HQ}, uint8_t{0}, false)));
	CHECK_THROWS(Integration::CaptureNativeIntentProof(Commands::BuildObjectArea,
		Data<Commands::BuildObjectArea>(TileIndex(Map::Size()), TileIndex(Map::Size()), ObjectType{OBJECT_OWNED_LAND}, uint8_t{0}, false)));
	CHECK_THROWS(Integration::CaptureNativeIntentProof(Commands::BuildObjectArea,
		Data<Commands::BuildObjectArea>(tile, tile, ObjectType{OBJECT_OWNED_LAND}, uint8_t{255}, false)));
	CommandDataBuffer malformed = object;
	malformed.back() = 2; // A bool must retain its canonical native encoding.
	CHECK_THROWS(Integration::CaptureNativeIntentProof(Commands::BuildObjectArea, malformed));
	malformed = object;
	malformed.push_back(0);
	CHECK_THROWS(Integration::CaptureNativeIntentProof(Commands::BuildObjectArea, malformed));
	SetTileHeight(tile, 1);
	CHECK_FALSE(Integration::VerifyNativeIntentProof(Commands::BuildObjectArea, object, world.Watermarks(), before.tile_proof, before.state).accepted);
}

TEST_CASE("Native one-point house capture preserves the real area quota gate", "[integration][quota][point-picker]")
{
	QuotaWorld world;
	const TileIndex tile = TileXY(32, 32);
	MakeClear(tile, ClearGround::Grass, 3);
	SetTileHeight(tile, 0);
	SetTileHeight(tile + TileDiffXY(1, 0), 0);
	SetTileHeight(tile + TileDiffXY(0, 1), 0);
	SetTileHeight(tile + TileDiffXY(1, 1), 0);
	const auto previous_mode = _game_mode;
	const auto previous_policy = _settings_game.economy.place_houses;
	const auto original_specs = HouseSpec::Specs().size();
	struct RestoreHouseContext {
		GameMode mode; PlaceHouses policy; size_t size;
		~RestoreHouseContext() { _game_mode = this->mode; _settings_game.economy.place_houses = this->policy; HouseSpec::Specs().resize(this->size); }
	} restore{previous_mode, previous_policy, original_specs};
	_game_mode = GameMode::Normal;
	_settings_game.economy.place_houses = PlaceHouses::AllowedConstructed;
	REQUIRE(original_specs + 2 < NUM_HOUSES);
	HouseSpec one{};
	one.enabled = true;
	one.building_flags.Set(BuildingFlag::Size1x1);
	HouseSpec multi{};
	multi.enabled = true;
	multi.building_flags.Set(BuildingFlag::Size2x2);
	HouseSpec::Specs().push_back(one);
	HouseSpec::Specs().push_back(multi);
	const HouseID type = static_cast<HouseID>(original_specs);
	const auto command = Data<Commands::PlaceHouseArea>(tile, tile, type, true, false, false);
	const auto point = Data<Commands::PlaceHouse>(tile, type, true, false);
	REQUIRE(Integration::IsNativeCommandForAction("house.build", Commands::PlaceHouseArea));
	const auto proof = Integration::CaptureNativeIntentProof(Commands::PlaceHouseArea, command);
	const auto point_proof = Integration::CaptureNativeIntentProof(Commands::PlaceHouse, point);
	CHECK(proof.tile_proof.at("count") == point_proof.tile_proof.at("count"));
	CHECK(proof.tile_proof != point_proof.tile_proof);
	CHECK(proof.state == Integration::NativeIntentState(Commands::PlaceHouse, point));
	CHECK(Integration::VerifyNativeIntentProof(Commands::PlaceHouseArea, command, world.Watermarks(), proof.tile_proof, proof.state).accepted);
	CHECK(Integration::VerifyNativeIntentProof(Commands::PlaceHouse, point, world.Watermarks(), point_proof.tile_proof, point_proof.state).accepted);
	CHECK_THROWS(Integration::CaptureNativeIntentProof(Commands::PlaceHouseArea,
		Data<Commands::PlaceHouseArea>(tile, tile + TileDiffXY(1, 0), type, true, false, false)));
	CHECK_THROWS(Integration::CaptureNativeIntentProof(Commands::PlaceHouseArea,
		Data<Commands::PlaceHouseArea>(tile, tile, HouseID(type + 1), true, false, false)));
	REQUIRE(Town::CanAllocateItem());
	std::unique_ptr<Town> town(Town::Create(tile));
	world.company->clear_limit = UINT32_MAX; // Isolate the area's object quota from normal clearing limits.
	world.company->build_object_limit = 0;
	/* Do not substitute PlaceHouse: the genuine native area click must retain
	 * this extra quota failure, even though the direct point command is legal. */
	REQUIRE(CmdPlaceHouse({}, tile, type, true, false).Succeeded());
	CHECK(CmdPlaceHouseArea({}, tile, tile, type, true, false, false).Failed());
	world.company->build_object_limit = uint32_t{1} << 16;
	const auto quota = world.company->build_object_limit;
	const auto money = world.company->money;
	const auto random = _random;
	const auto native = CmdPlaceHouseArea({}, tile, tile, type, true, false, false);
	REQUIRE(native.Succeeded());
	CHECK(native.GetCost() == CmdPlaceHouse({}, tile, type, true, false).GetCost());
	CHECK(world.company->build_object_limit == quota);
	CHECK(world.company->money == money);
	CHECK(_random.state[0] == random.state[0]);
	CHECK(_random.state[1] == random.state[1]);
}

TEST_CASE("One-point native object capture keeps linked clear effects and object incarnation fencing", "[integration][quota][point-picker]")
{
	QuotaWorld world;
	ResetObjects();
	const TileIndex tile = TileXY(32, 32);
	const TileArea old_footprint(tile, 2, 2);
	REQUIRE(Object::CanAllocateItem());
	std::unique_ptr<Object> object(Object::Create(OBJECT_HQ, nullptr, old_footprint, TimerGameCalendar::date, uint8_t{0}));
	for (TileIndex old : old_footprint) MakeObject(old, _current_company, object->index, WaterClass::Invalid, 0);
	const auto command = Data<Commands::BuildObjectArea>(tile, tile, ObjectType{OBJECT_OWNED_LAND}, uint8_t{0}, false);
	const auto proof = Integration::CaptureNativeIntentProof(Commands::BuildObjectArea, command);
	auto source = world.Watermarks();
	source.object = Object::GetLastInstanceGeneration();
	CHECK(proof.tile_proof.at("count") == 4);
	CHECK(Integration::VerifyNativeIntentProof(Commands::BuildObjectArea, command, source, proof.tile_proof, proof.state).accepted);
	Tile(tile + TileDiffXY(1, 1)).m1() ^= 1;
	CHECK_FALSE(Integration::VerifyNativeIntentProof(Commands::BuildObjectArea, command, source, proof.tile_proof, proof.state).accepted);
	Tile(tile + TileDiffXY(1, 1)).m1() ^= 1;
	const ObjectID old_id = object->index;
	object.reset();
	REQUIRE(Object::CanAllocateItem());
	object.reset(Object::Create(OBJECT_HQ, nullptr, old_footprint, TimerGameCalendar::date, uint8_t{0}));
	REQUIRE(object->index == old_id);
	REQUIRE(object->GetInstanceGeneration() > source.object);
	for (TileIndex old : old_footprint) MakeObject(old, _current_company, object->index, WaterClass::Invalid, 0);
	CHECK_FALSE(Integration::VerifyNativeIntentProof(Commands::BuildObjectArea, command, source, proof.tile_proof, proof.state).accepted);
}
