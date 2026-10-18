/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License, version 2.
 */

/** @file test_integration_depot_direction.cpp Local depot facing selection and frozen dependencies. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions_internal.h"
#include "../integration/visual_commands.h"
#include "../clear_map.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../core/endian_func.hpp"
#include "../core/pool_func.hpp"
#include "../core/random_func.hpp"
#include "../depot_base.h"
#include "../depot_func.h"
#include "../economy_func.h"
#include "../engine_base.h"
#include "../genworld.h"
#include "../industry.h"
#include "../landscape.h"
#include "../language.h"
#include "../network/network.h"
#include "../rail.h"
#include "../rail_cmd.h"
#include "../rail_map.h"
#include "../road.h"
#include "../road_cmd.h"
#include "../road_map.h"
#include "../settings_type.h"
#include "../station_base.h"
#include "../strings_func.h"
#include "../town.h"
#include "../town_kdtree.h"
#include "../vehicle_base.h"

#include <chrono>
#include <fstream>
#include <thread>

#include "../safeguards.h"

extern RoadTypes _roadtypes_road;
extern RoadTypes _roadtypes_tram;

namespace {

using Integration::Json;
using Integration::Detail::ActionInvocation;
constexpr RoadType OTHER_ROADTYPE = static_cast<RoadType>(2);

std::array<uint16_t, 10> TileBytes(TileIndex index)
{
	Tile tile(index);
	return {tile.type(), tile.height(), tile.m1(), tile.m2(), tile.m3(), tile.m4(), tile.m5(), tile.m6(), tile.m7(), tile.m8()};
}

/** Match the existing Shift fixture's native lifecycle without starting a server. */
struct DepotDirectionWorld {
	CompanyID previous_company = _current_company;
	CompanyID previous_local = _local_company;
	GameMode previous_mode = _game_mode;
	bool previous_networking = _networking;
	bool previous_server = _network_server;
	bool previous_generating = _generating_world;
	decltype(_settings_game.construction) previous_construction = _settings_game.construction;
	RoadTypes previous_roads = _roadtypes_road;
	RoadTypes previous_trams = _roadtypes_tram;
	Money previous_road_price = _price[Price::BuildDepotRoad];
	Money previous_rail_price = _price[Price::BuildDepotTrain];
	Money previous_track_price = _price[Price::BuildRail];
	Money previous_clear_price = _price[Price::ClearGrass];
	SavedRandomSeeds previous_seeds;
	Company *company;
	TileIndex tile{};

	static void CleanPools()
	{
		_vehicle_pool.CleanPool();
		_engine_pool.CleanPool();
		_depot_pool.CleanPool();
		_station_pool.CleanPool();
		_industry_pool.CleanPool();
		_town_pool.CleanPool();
		_company_pool.CleanPool();
		_town_kdtree.Clear();
		_town_local_authority_kdtree.Clear();
	}

	DepotDirectionWorld()
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
		CleanPools();
		Map::Allocate(64, 64);
		_settings_game.construction.freeform_edges = false;
		_settings_game.construction.build_on_slopes = true;
		InitializeLandscape();
		_game_mode = GameMode::Normal;
		_networking = true;
		_network_server = true;
		_generating_world = false;
		_local_company = COMPANY_SPECTATOR;
		_roadtypes_road.Set(ROADTYPE_ROAD);
		_roadtypes_road.Reset(ROADTYPE_TRAM);
		_roadtypes_tram.Set(ROADTYPE_TRAM);
		_roadtypes_tram.Reset(ROADTYPE_ROAD);
		/* Artificial neighbour type used to check exact-type rejection. Native
		 * map setters require its category even though the actor never builds it. */
		_roadtypes_road.Set(OTHER_ROADTYPE);
		_roadtypes_tram.Reset(OTHER_ROADTYPE);
		_price[Price::BuildDepotRoad] = 123;
		_price[Price::BuildDepotTrain] = 321;
		_price[Price::BuildRail] = 8;
		_price[Price::ClearGrass] = 0;
		REQUIRE(Company::CanAllocateItem());
		this->company = Company::Create();
		_current_company = this->company->index;
		this->company->money = 1'000'000;
		this->company->avail_roadtypes = {ROADTYPE_ROAD, ROADTYPE_TRAM};
		this->company->avail_railtypes = {RAILTYPE_RAIL};
		this->company->clear_limit = UINT32_MAX;
		this->tile = TileXY(20, 20);
		_random.SetSeed(0x12345678);
		_interactive_random.SetSeed(0x87654321);
	}

	~DepotDirectionWorld()
	{
		CleanPools();
		_current_company = this->previous_company;
		_local_company = this->previous_local;
		_game_mode = this->previous_mode;
		_networking = this->previous_networking;
		_network_server = this->previous_server;
		_generating_world = this->previous_generating;
		_settings_game.construction = this->previous_construction;
		_roadtypes_road = this->previous_roads;
		_roadtypes_tram = this->previous_trams;
		_price[Price::BuildDepotRoad] = this->previous_road_price;
		_price[Price::BuildDepotTrain] = this->previous_rail_price;
		_price[Price::BuildRail] = this->previous_track_price;
		_price[Price::ClearGrass] = this->previous_clear_price;
		RestoreRandomSeeds(this->previous_seeds);
	}

	TileIndex Neighbour(DiagDirection direction) const { return AddTileIndexDiffCWrap(this->tile, TileIndexDiffCByDiagDir(direction)); }
	Json Parameters() const { return {{"tile", this->tile.base()}, {"direction", "auto"}}; }

	void Road(DiagDirection direction, RoadType road = ROADTYPE_ROAD, RoadType tram = INVALID_ROADTYPE, Owner owner = OWNER_NONE) const
	{
		MakeRoadNormal(this->Neighbour(direction), DiagDirToRoadBits(direction), road, tram, TownID::Invalid(), owner, owner);
	}

	void Rail(DiagDirection direction, RailType type = RAILTYPE_RAIL, TrackBits tracks = TrackBits{Track::X}) const
	{
		MakeRailNormal(this->Neighbour(direction), this->company->index, tracks, type);
	}

	std::vector<std::array<uint16_t, 10>> MapBytes() const
	{
		std::vector<std::array<uint16_t, 10>> result;
		for (uint index = 0; index < Map::Size(); ++index) result.push_back(TileBytes(TileIndex(index)));
		return result;
	}

	/** Exercise the real resolver/Shift path without the public estimate rate gate. */
	Json Quote(const char *action, const Json &parameters) const
	{
		const auto bytes = this->MapBytes();
		const Money money = this->company->money;
		const auto depots = Depot::GetNumItems();
		const auto pending = Integration::PendingLocalCommands();
		SavedRandomSeeds seeds;
		SaveRandomSeeds(&seeds);
		ActionInvocation invocation(ActionInvocation::Mode::Estimate);
		const auto prepared = Integration::Detail::DispatchConstructionAction(action, parameters, invocation);
		REQUIRE(prepared.has_value());
		CHECK(this->MapBytes() == bytes);
		CHECK(this->company->money == money);
		CHECK(Depot::GetNumItems() == depots);
		CHECK(Integration::PendingLocalCommands() == pending);
		CHECK(_random.state[0] == seeds.random.state[0]);
		CHECK(_random.state[1] == seeds.random.state[1]);
		CHECK(_interactive_random.state[0] == seeds.interactive_random.state[0]);
		CHECK(_interactive_random.state[1] == seeds.interactive_random.state[1]);
		if (prepared->prequeue_result) {
			CHECK(invocation.Calls() == 0);
			return *prepared->prequeue_result;
		}
		REQUIRE(invocation.Calls() == 1);
		REQUIRE(invocation.GetEstimate().has_value());
		const auto &native = *invocation.GetEstimate();
		return Integration::FormatCommandResult(native.command, native.cost, {});
	}
};

} // namespace

TEST_CASE("Auto depots face each unique neighbour without adding a reciprocal connection", "[integration][construction][depot-direction]")
{
	for (bool rail : {false, true}) {
		for (DiagDirection direction : EnumRange(DiagDirection::End)) {
			DYNAMIC_SECTION((rail ? "rail" : "road") << " direction " << to_underlying(direction)) {
				DepotDirectionWorld world;
				const char *action = rail ? "rail.depot.build" : "road.depot.build";
				if (rail) world.Rail(direction); else world.Road(direction, ROADTYPE_ROAD, INVALID_ROADTYPE, OWNER_TOWN);
				const auto neighbour = world.Neighbour(direction);
				const auto original = TileBytes(neighbour);
				const Json parameters = world.Parameters();
				world.company->money = 0; // Candidate legality and quotes do not require funds.
				const Json quote = world.Quote(action, parameters);
				REQUIRE(quote.at("success") == true);
				CHECK_FALSE(quote.contains("direction"));
				CHECK_FALSE(quote.contains("depot_id"));
				world.company->money = 1'000'000;
				const Money before = world.company->money;
				const auto pending = Integration::PendingLocalCommands();
				_networking = false; // Same ordinary synchronous native Post as the estimate fixture.
				REQUIRE(Integration::DispatchAction(action, parameters).accepted);
				CHECK(Depot::GetNumItems() == 1);
				CHECK(before - world.company->money == Money(quote.at("cost").get<int64_t>()));
				CHECK(Integration::PendingLocalCommands() == pending);
				CHECK(TileBytes(neighbour) == original);
				CHECK((rail ? GetRailDepotDirection(world.tile) : GetRoadDepotDirection(world.tile)) == direction);
				if (!rail) CHECK_FALSE(GetRoadBits(neighbour, RoadTramType::Road).Any(DiagDirToRoadBits(ReverseDiagDir(direction))));
				else CHECK(GetTrackBits(neighbour) == TrackBits{Track::X}); // No entrance turn was added.
				const Commands command = rail ? Commands::BuildRailDepot : Commands::BuildRoadDepot;
				const Json receipt = Integration::FormatCommandResult(command, CommandCost(ExpensesType::Construction, before - world.company->money), {}, {{"name", action}, {"parameters", parameters}});
				CHECK(receipt.at("success") == true);
				CHECK(receipt.at("direction") == to_underlying(direction));
				CHECK(receipt.at("depot_id") == Integration::Detail::Handle(Depot::GetByTile(world.tile)));
			}
		}
	}
}

TEST_CASE("Auto depot candidates require exact ordinary transport type and nonempty category bits", "[integration][construction][depot-direction]")
{
	DepotDirectionWorld world;
	const char *action = "road.depot.build";
	Json parameters = world.Parameters();
	std::string expected = "depot_direction_unavailable";
	SECTION("no neighbour") {}
	SECTION("multiple road neighbours") { world.Road(DiagDirection::NE); world.Road(DiagDirection::SE); expected = "depot_direction_ambiguous"; }
	SECTION("another road type") { world.Road(DiagDirection::NE, OTHER_ROADTYPE); }
	SECTION("only tram category is present") { world.Road(DiagDirection::NE, INVALID_ROADTYPE, ROADTYPE_TRAM); }
	SECTION("selected road category is empty while tram bits are present") {
		world.Road(DiagDirection::NE, ROADTYPE_ROAD, ROADTYPE_TRAM);
		SetRoadBits(world.Neighbour(DiagDirection::NE), {}, RoadTramType::Road);
	}
	SECTION("road depot is not a normal road neighbour") {
		const auto neighbour = world.Neighbour(DiagDirection::NE);
		REQUIRE(Depot::CanAllocateItem());
		const auto *depot = Depot::Create(neighbour);
		MakeRoadDepot(neighbour, world.company->index, depot->index, DiagDirection::NE, ROADTYPE_ROAD);
	}
	SECTION("foreign plain rail") {
		action = "rail.depot.build";
		world.Rail(DiagDirection::NE);
		REQUIRE(Company::CanAllocateItem());
		SetTileOwner(world.Neighbour(DiagDirection::NE), Company::Create()->index);
	}
	SECTION("another rail type") { action = "rail.depot.build"; world.Rail(DiagDirection::NE, RAILTYPE_MONO); }
	SECTION("empty plain rail") { action = "rail.depot.build"; world.Rail(DiagDirection::NE, RAILTYPE_RAIL, {}); }
	SECTION("multiple rail neighbours") { action = "rail.depot.build"; world.Rail(DiagDirection::NE); world.Rail(DiagDirection::SE); expected = "depot_direction_ambiguous"; }
	SECTION("rail depot is not plain rail") {
		action = "rail.depot.build";
		const auto neighbour = world.Neighbour(DiagDirection::NE);
		REQUIRE(Depot::CanAllocateItem());
		const auto *depot = Depot::Create(neighbour);
		MakeRailDepot(neighbour, world.company->index, depot->index, DiagDirection::NE, RAILTYPE_RAIL);
	}
	const Json result = world.Quote(action, parameters);
	CHECK(result.at("success") == false);
	CHECK(result.at("cost") == 0);
	CHECK(result.at("error").at("code") == expected);
	CHECK_FALSE(result.contains("direction"));
	CHECK_FALSE(result.contains("depot_id"));
	const auto pending = Integration::PendingLocalCommands();
	const auto bytes = world.MapBytes();
	const Money money = world.company->money;
	const auto rejected = Integration::DispatchAction(action, parameters);
	CHECK_FALSE(rejected.accepted);
	CHECK(rejected.code == expected);
	REQUIRE(rejected.prequeue_result.has_value());
	CHECK(*rejected.prequeue_result == result);
	CHECK(Integration::PendingLocalCommands() == pending);
	CHECK(world.MapBytes() == bytes);
	CHECK(world.company->money == money);
}

TEST_CASE("Auto tram facing uses the selected category on a mixed road tile", "[integration][construction][depot-direction]")
{
	DepotDirectionWorld world;
	world.Road(DiagDirection::NE, ROADTYPE_ROAD, ROADTYPE_TRAM);
	SetRoadBits(world.Neighbour(DiagDirection::NE), {}, RoadTramType::Road);
	Json parameters = world.Parameters();
	parameters["road_type"] = ROADTYPE_TRAM;
	CHECK(world.Quote("road.depot.build", parameters).at("success") == true);
	SetRoadBits(world.Neighbour(DiagDirection::NE), {}, RoadTramType::Tram);
	CHECK(world.Quote("road.depot.build", parameters).at("error").at("code") == "depot_direction_unavailable");
}

TEST_CASE("Auto depot selection filters native slope legality before resolving ambiguity", "[integration][construction][depot-direction]")
{
	DepotDirectionWorld world;
	SetTileHeight(world.tile, 1);
	const Slope slope = GetTileSlope(world.tile);
	REQUIRE(slope != SLOPE_FLAT);
	DiagDirection legal = DiagDirection::Invalid, illegal = DiagDirection::Invalid;
	for (DiagDirection direction : EnumRange(DiagDirection::End)) {
		if (CanBuildDepotByTileh(direction, slope)) legal = direction; else illegal = direction;
	}
	REQUIRE(legal != DiagDirection::Invalid);
	REQUIRE(illegal != DiagDirection::Invalid);
	world.Road(legal);
	world.Road(illegal);
	CHECK(world.Quote("road.depot.build", world.Parameters()).at("success") == true);
	_settings_game.construction.build_on_slopes = false;
	CHECK(world.Quote("road.depot.build", world.Parameters()).at("error").at("code") == "depot_direction_unavailable");
}

TEST_CASE("Auto depot enumeration handles every map edge without wrapping", "[integration][construction][depot-direction]")
{
	for (DiagDirection direction : EnumRange(DiagDirection::End)) {
		DYNAMIC_SECTION("edge " << to_underlying(direction)) {
			DepotDirectionWorld world;
			switch (direction) {
				case DiagDirection::NE: world.tile = TileXY(0, 20); break;
				case DiagDirection::SE: world.tile = TileXY(20, Map::MaxY() - 1); break;
				case DiagDirection::SW: world.tile = TileXY(Map::MaxX() - 1, 20); break;
				case DiagDirection::NW: world.tile = TileXY(20, 0); break;
				default: NOT_REACHED();
			}
			CHECK(world.Quote("road.depot.build", world.Parameters()).at("error").at("code") == "depot_direction_unavailable");
			world.Road(ReverseDiagDir(direction));
			CHECK(world.Quote("road.depot.build", world.Parameters()).at("success") == true);
		}
	}
}

TEST_CASE("Auto depot queued targets fingerprint all local choices without retesting", "[integration][construction][depot-direction]")
{
	DepotDirectionWorld world;
	world.Road(DiagDirection::NE);
	const Json parameters = world.Parameters();
	const auto pending = Integration::PendingLocalCommands();
	const auto bytes = world.MapBytes();
	const auto seeds = _random;
	const auto interactive = _interactive_random;
	const Json captured = Integration::CaptureActionTargets("road.depot.build", parameters);
	REQUIRE(captured.is_array());
	CHECK(world.MapBytes() == bytes);
	CHECK(Integration::PendingLocalCommands() == pending);
	CHECK(_random.state[0] == seeds.state[0]);
	CHECK(_random.state[1] == seeds.state[1]);
	CHECK(_interactive_random.state[0] == interactive.state[0]);
	CHECK(_interactive_random.state[1] == interactive.state[1]);
	CHECK(Integration::ValidateActionTargets("road.depot.build", parameters, captured).accepted);
	SECTION("new second candidate") { world.Road(DiagDirection::SE); }
	SECTION("neighbour road bits") { SetRoadBits(world.Neighbour(DiagDirection::NE), ROAD_X, RoadTramType::Road); }
	SECTION("neighbour road type") { SetRoadTypeRoad(world.Neighbour(DiagDirection::NE), OTHER_ROADTYPE); }
	SECTION("neighbour category ownership") { SetRoadOwner(world.Neighbour(DiagDirection::NE), RoadTramType::Road, OWNER_TOWN); }
	SECTION("previously empty neighbour height") { SetTileHeight(world.Neighbour(DiagDirection::SW), 1); }
	SECTION("center slope") { SetTileHeight(world.tile, 1); }
	SECTION("build setting") { _settings_game.construction.build_on_slopes = false; }
	const auto validation = Integration::ValidateActionTargets("road.depot.build", parameters, captured);
	CHECK_FALSE(validation.accepted);
	CHECK(validation.code == "stale_target");
}

TEST_CASE("Explicit depot directions bypass auto candidates and local choice fences", "[integration][construction][depot-direction]")
{
	DepotDirectionWorld world;
	for (const char *action : {"road.depot.build", "rail.depot.build"}) {
		for (int direction = 0; direction < 4; ++direction) {
			Json parameters = world.Parameters();
			parameters["direction"] = direction;
			CHECK(world.Quote(action, parameters).at("success") == true); // No neighbours.
			const Json captured = Integration::CaptureActionTargets(action, parameters);
			world.Road(DiagDirection::NE);
			world.Road(DiagDirection::SE);
			CHECK(Integration::ValidateActionTargets(action, parameters, captured).accepted);
			CHECK(world.Quote(action, parameters).at("success") == true); // Ambiguous neighbours.
			MakeClear(world.Neighbour(DiagDirection::NE), ClearGround::Grass, 3);
			MakeClear(world.Neighbour(DiagDirection::SE), ClearGround::Grass, 3);
		}
	}
}

TEST_CASE("Auto rail queued targets retain owner type tracks and depot incarnation", "[integration][construction][depot-direction]")
{
	DepotDirectionWorld world;
	world.Rail(DiagDirection::NE);
	REQUIRE(Depot::CanAllocateItem());
	const auto *depot = Depot::Create(world.tile);
	MakeRailDepot(world.tile, world.company->index, depot->index, DiagDirection::NE, RAILTYPE_RAIL);
	const Json parameters = world.Parameters();
	const Json captured = Integration::CaptureActionTargets("rail.depot.build", parameters);
	REQUIRE(captured.is_array());
	REQUIRE(Integration::ValidateActionTargets("rail.depot.build", parameters, captured).accepted);
	SECTION("owner") {
		REQUIRE(Company::CanAllocateItem());
		SetTileOwner(world.Neighbour(DiagDirection::NE), Company::Create()->index);
	}
	SECTION("rail type") { SetRailType(world.Neighbour(DiagDirection::NE), RAILTYPE_MONO); }
	SECTION("track bits") { SetTrackBits(world.Neighbour(DiagDirection::NE), TrackBits{Track::Y}); }
	SECTION("new second candidate") { world.Rail(DiagDirection::SE); }
	SECTION("center rotation") { SetRailDepotExitDirection(world.tile, DiagDirection::SW); }
	SECTION("center depot replacement") {
		const auto index = depot->index;
		const auto generation = depot->GetInstanceGeneration();
		/* Pool cleanup suppresses depot destruction UI; rebuild the same tile
		 * and numeric slot to isolate its monotonic incarnation fence. */
		_depot_pool.CleanPool();
		REQUIRE(Depot::CanAllocateItem());
		const auto *replacement = Depot::Create(world.tile);
		REQUIRE(replacement->index == index);
		REQUIRE(replacement->GetInstanceGeneration() != generation);
		MakeRailDepot(world.tile, world.company->index, replacement->index, DiagDirection::NE, RAILTYPE_RAIL);
	}
	const auto validation = Integration::ValidateActionTargets("rail.depot.build", parameters, captured);
	CHECK_FALSE(validation.accepted);
	CHECK(validation.code == "stale_target");
}

TEST_CASE("Public depot estimates preserve safe auto selection failure envelopes", "[integration][construction][depot-direction]")
{
	DepotDirectionWorld world;
	std::string expected = "depot_direction_unavailable";
	SECTION("unavailable") {}
	SECTION("ambiguous") { world.Road(DiagDirection::NE); world.Road(DiagDirection::SE); expected = "depot_direction_ambiguous"; }
	/* The public estimate gate is deliberately shared across all native tests. */
	std::this_thread::sleep_for(std::chrono::milliseconds(1010));
	const auto pending = Integration::PendingLocalCommands();
	const Json quote = Integration::EstimateAction("road.depot.build", world.Parameters());
	REQUIRE(quote.contains("estimate"));
	const Json result = quote.at("estimate");
	CHECK(result.at("success") == false);
	CHECK(result.at("cost") == 0);
	CHECK(result.at("advisory") == true);
	CHECK(result.at("test_and_execute_may_differ") == false);
	CHECK(result.at("error").at("code") == expected);
	CHECK_FALSE(result.contains("direction"));
	CHECK_FALSE(result.contains("depot_id"));
	CHECK(Integration::PendingLocalCommands() == pending);
}

TEST_CASE("Depot result direction comes from the owned completed family including rotations", "[integration][construction][depot-direction]")
{
	DepotDirectionWorld world;
	Json parameters = world.Parameters();
	parameters["direction"] = 0;
	_networking = false;
	REQUIRE(Integration::DispatchAction("road.depot.build", parameters).accepted);
	const Json action = {{"name", "road.depot.build"}, {"parameters", parameters}};
	parameters["direction"] = 2;
	REQUIRE(Integration::DispatchAction("road.depot.build", parameters).accepted);
	const Json rotated = Integration::FormatCommandResult(Commands::BuildRoadDepot, CommandCost{}, {}, action);
	CHECK(rotated.at("direction") == 2); // Original input was 0.
	CHECK(Depot::GetNumItems() == 1);
	REQUIRE(Integration::DispatchAction("road.depot.build", parameters).accepted); // Same direction/no-op.
	CHECK(Integration::FormatCommandResult(Commands::BuildRoadDepot, CommandCost{}, {}, action).at("direction") == 2);
	CHECK_FALSE(Integration::FormatCommandResult(Commands::BuildRailDepot, CommandCost{}, {}, action).contains("direction"));
	CHECK_FALSE(Integration::FormatCommandResult(Commands::BuildRailDepot, CommandCost{}, {}, {{"name", "rail.depot.build"}, {"parameters", parameters}}).contains("direction"));
	CHECK_FALSE(Integration::FormatCommandResult(Commands::BuildRoadDepot, CMD_ERROR, {}, action).contains("direction"));
	CHECK_FALSE(Integration::FormatCommandResult(Commands::BuildRoadDepot, CommandCost{}, {}).contains("direction")); // Quote.
	REQUIRE(Company::CanAllocateItem());
	SetTileOwner(world.tile, Company::Create()->index);
	CHECK_FALSE(Integration::FormatCommandResult(Commands::BuildRoadDepot, CommandCost{}, {}, action).contains("direction"));
}
