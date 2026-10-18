/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file test_integration_pause.cpp Native pause policy and shared control metadata. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/integration_bridge.h"
#include "../clear_map.h"
#include "../command_func.h"
#include "../company_base.h"
#include "../company_cmd.h"
#include "../company_func.h"
#include "../core/endian_func.hpp"
#include "../core/pool_func.hpp"
#include "../core/random_func.hpp"
#include "../economy_func.h"
#include "../genworld.h"
#include "../gfx_func.h"
#include "../group_cmd.h"
#include "../language.h"
#include "../misc_cmd.h"
#include "../network/network.h"
#include "../openttd.h"
#include "../road.h"
#include "../road_cmd.h"
#include "../road_map.h"
#include "../settings_cmd.h"
#include "../settings_type.h"
#include "../signs_cmd.h"
#include "../timer/timer_game_tick.h"
#include "../vehicle_cmd.h"

#include <filesystem>
#include <fstream>

#include "../safeguards.h"

extern RoadTypeInfo _roadtypes[ROADTYPE_END];
extern RoadTypes _roadtypes_road;
extern RoadTypes _roadtypes_tram;

namespace {

using Integration::Json;

/** These tests never initialize IPC or impersonate an authenticated bridge session. */
struct PauseGlobals {
	PauseModes pause = _pause_mode;
	CommandPauseLevel policy = _settings_game.construction.command_pause_level;
	GameMode mode = _game_mode;
	CompanyID current_company = _current_company;
	CompanyID local_company = _local_company;
	bool networking = _networking;
	bool network_server = _network_server;
	bool shift = _shift_pressed;
	bool generating = _generating_world;
	uint8_t error_duration = _settings_client.gui.errmsg_duration;
	Randomizer random = _random;
	Randomizer interactive_random = _interactive_random;

	PauseGlobals()
	{
		_game_mode = GameMode::Normal;
		_networking = false;
		_network_server = false;
		_shift_pressed = false;
		_generating_world = false;
		/* Native Info errors return before creating a window. Successful world
		 * commands do not need a local cost animation in this company fixture. */
		_settings_client.gui.errmsg_duration = 0;
		_local_company = COMPANY_SPECTATOR;
	}

	~PauseGlobals()
	{
		_pause_mode = this->pause;
		_settings_game.construction.command_pause_level = this->policy;
		_game_mode = this->mode;
		_current_company = this->current_company;
		_local_company = this->local_company;
		_networking = this->networking;
		_network_server = this->network_server;
		_shift_pressed = this->shift;
		_generating_world = this->generating;
		_settings_client.gui.errmsg_duration = this->error_duration;
		_random = this->random;
		_interactive_random = this->interactive_random;
	}
};

struct PausedCompany : PauseGlobals {
	Prices prices = _price;
	RoadTypeInfo road = _roadtypes[ROADTYPE_ROAD];
	RoadTypes road_mask = _roadtypes_road;
	RoadTypes tram_mask = _roadtypes_tram;
	Company *company = nullptr;
	TileIndex tile;

	PausedCompany()
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
		REQUIRE_FALSE(Integration::RendererIsActive());
		Map::Allocate(64, 64);
		for (uint y = 0; y < Map::SizeY(); ++y) {
			for (uint x = 0; x < Map::SizeX(); ++x) {
				const TileIndex candidate = TileXY(x, y);
				SetTileHeight(candidate, 0);
				if (IsInnerTile(candidate)) MakeClear(candidate, ClearGround::Grass, 3);
			}
		}
		REQUIRE(Company::CanAllocateItem());
		this->company = Company::Create();
		_current_company = this->company->index;
		this->company->money = 1000000;
		this->company->max_loan = 1000000;
		this->company->clear_limit = 100U << 16;
		this->company->avail_roadtypes = {ROADTYPE_ROAD};
		this->tile = TileXY(20, 20);
		_roadtypes[ROADTYPE_ROAD] = RoadTypeInfo{};
		_roadtypes[ROADTYPE_ROAD].cost_multiplier = 8;
		_roadtypes_road.Set(ROADTYPE_ROAD);
		_roadtypes_tram.Reset(ROADTYPE_ROAD);
		_price[Price::BuildRoad] = 113;
		_price[Price::ClearGrass] = 0;
		_pause_mode = {PauseMode::Normal};
	}

	~PausedCompany()
	{
		delete this->company;
		_price = this->prices;
		_roadtypes[ROADTYPE_ROAD] = this->road;
		_roadtypes_road = this->road_mask;
		_roadtypes_tram = this->tram_mask;
	}
};

uint64_t Revision(const Json &status)
{
	REQUIRE(status.is_object());
	REQUIRE(status.size() == 4);
	REQUIRE(status.at("paused").is_boolean());
	REQUIRE(status.at("reasons").is_array());
	REQUIRE(status.at("command_policy").is_string());
	REQUIRE(status.at("revision").is_number_unsigned());
	return status.at("revision").get<uint64_t>();
}

void CheckRandom(const Randomizer &random, const Randomizer &interactive)
{
	CHECK(_random.state[0] == random.state[0]);
	CHECK(_random.state[1] == random.state[1]);
	CHECK(_interactive_random.state[0] == interactive.state[0]);
	CHECK(_interactive_random.state[1] == interactive.state[1]);
}

} // namespace

TEST_CASE("Paused native command categories follow all four configured policies", "[integration][pause]")
{
	PauseGlobals globals;
	_pause_mode = {PauseMode::Normal};
	/* These are real native commands from all nine categories, including the
	 * company/server/cheat exceptions under NoActions. This does not expose
	 * those commands through the public integration allowlist. */
	struct Case {
		Commands command;
		std::array<bool, 4> allowed;
	};
	static constexpr std::array<Case, 9> cases = {{
		{Commands::BuildRoad, {false, false, false, true}},
		{Commands::BuildVehicle, {false, false, true, true}},
		{Commands::IncreaseLoan, {false, false, true, true}},
		{Commands::StartStopVehicle, {false, true, true, true}},
		{Commands::CreateGroup, {false, true, true, true}},
		{Commands::RenameSign, {false, true, true, true}},
		{Commands::ChangeCompanySetting, {true, true, true, true}},
		{Commands::Pause, {true, true, true, true}},
		{Commands::MoneyCheat, {true, true, true, true}},
	}};
	static constexpr std::array policies = {CommandPauseLevel::NoActions, CommandPauseLevel::NoConstruction,
		CommandPauseLevel::NoLandscaping, CommandPauseLevel::AllActions};
	for (size_t i = 0; i < policies.size(); ++i) {
		_settings_game.construction.command_pause_level = policies[i];
		for (const Case &entry : cases) {
			INFO(GetCommandName(entry.command));
			INFO(i);
			CHECK(IsCommandAllowedWhilePaused(entry.command) == entry.allowed[i]);
		}
	}
	_game_mode = GameMode::Editor;
	_settings_game.construction.command_pause_level = CommandPauseLevel::NoActions;
	for (const Case &entry : cases) CHECK(IsCommandAllowedWhilePaused(entry.command));
}

TEST_CASE("Fresh native Posts reject forbidden work without mutation or charge", "[integration][pause]")
{
	PausedCompany world;
	_settings_game.construction.command_pause_level = CommandPauseLevel::NoConstruction;
	const Money money = world.company->money;
	const Money loan = world.company->current_loan;
	const auto expenses = world.company->yearly_expenses;
	REQUIRE(Command<Commands::IncreaseLoan>::Do({}, LoanCommand::Amount, Money{10000}).Succeeded());
	CHECK_FALSE(Command<Commands::IncreaseLoan>::Post(LoanCommand::Amount, Money{10000}));
	CHECK(world.company->money == money);
	CHECK(world.company->current_loan == loan);
	CHECK(world.company->yearly_expenses == expenses);
	CHECK(_pause_mode == PauseModes{PauseMode::Normal});

	_settings_game.construction.command_pause_level = CommandPauseLevel::NoLandscaping;
	const CommandCost road_test = Command<Commands::BuildRoad>::Do(CommandFlagsToDCFlags(GetCommandFlags<Commands::BuildRoad>()),
		world.tile, ROAD_X, ROADTYPE_ROAD, {}, TownID::Invalid());
	REQUIRE(road_test.Succeeded());
	REQUIRE(road_test.GetCost() > 0);
	const auto last_build = world.company->last_build_coordinate;
	const auto clear_limit = world.company->clear_limit;
	CHECK_FALSE(Command<Commands::BuildRoad>::Post(world.tile, ROAD_X, ROADTYPE_ROAD, {}, TownID::Invalid()));
	CHECK(IsTileType(world.tile, TileType::Clear));
	CHECK(world.company->money == money);
	CHECK(world.company->current_loan == loan);
	CHECK(world.company->yearly_expenses == expenses);
	CHECK(world.company->last_build_coordinate == last_build);
	CHECK(world.company->clear_limit == clear_limit);
	CHECK(world.company->infrastructure.road[ROADTYPE_ROAD] == 0);
	CHECK(_pause_mode == PauseModes{PauseMode::Normal});

	_settings_game.construction.command_pause_level = CommandPauseLevel::AllActions;
	REQUIRE(Command<Commands::BuildRoad>::Post(world.tile, ROAD_X, ROADTYPE_ROAD, {}, TownID::Invalid()));
	CHECK(IsNormalRoadTile(world.tile));
	CHECK(GetRoadBits(world.tile, RoadTramType::Road) == ROAD_X);
	CHECK(world.company->money == money - road_test.GetCost());
	CHECK(world.company->infrastructure.road[ROADTYPE_ROAD] == ROAD_X.Count());
	CHECK(_pause_mode.Test(PauseMode::CommandDuringPause));
}

TEST_CASE("Permitted native company Posts work while paused and bookkeeping stays invisible", "[integration][pause]")
{
	PausedCompany world;
	_settings_game.construction.command_pause_level = CommandPauseLevel::NoActions;
	const Json before = Integration::CurrentPauseStatus();
	const uint64_t revision = Revision(before);
	const Money money = world.company->money;
	REQUIRE(Command<Commands::RenameCompany>::Post(std::string("Paused transport")));
	CHECK(world.company->name == "Paused transport");
	CHECK(world.company->money == money);
	CHECK(_pause_mode.Test(PauseMode::Normal));
	CHECK(_pause_mode.Test(PauseMode::CommandDuringPause));
	CHECK(Integration::CurrentPauseStatus() == before);
	CHECK(Revision(Integration::CurrentPauseStatus()) == revision);

	_settings_game.construction.command_pause_level = CommandPauseLevel::NoLandscaping;
	REQUIRE(Command<Commands::IncreaseLoan>::Post(LoanCommand::Amount, Money{10000}));
	CHECK(world.company->current_loan == 10000);
	CHECK(world.company->money == money + 10000);
	CHECK(Integration::CurrentPauseStatus().at("command_policy") == "no_landscaping");
}

TEST_CASE("Pause metadata reports ordered reasons and independent same-frame revisions", "[integration][pause]")
{
	PauseGlobals globals;
	const auto tick = TimerGameTick::counter;
	const CompanyID company = _current_company;
	const size_t company_count = Company::GetNumItems();
	const Randomizer random = _random;
	const Randomizer interactive = _interactive_random;
	_settings_game.construction.command_pause_level = CommandPauseLevel::NoActions;
	_pause_mode = {PauseMode::ActiveClients, PauseMode::Normal, PauseMode::LinkGraph, PauseMode::Join,
		PauseMode::GameScript, PauseMode::SaveLoad, PauseMode::Error};
	const Json all = Integration::CurrentPauseStatus();
	uint64_t revision = Revision(all);
	CHECK(revision > 0);
	CHECK(all.at("paused") == true);
	CHECK(all.at("reasons") == Json::array({"manual", "saving_loading", "joining", "error", "insufficient_clients", "game_script", "link_graph"}));
	CHECK(all.at("command_policy") == "no_actions");
	CHECK(Integration::CurrentPauseStatus() == all);
	_pause_mode.Set(PauseMode::CommandDuringPause);
	CHECK(Integration::CurrentPauseStatus() == all);
	_pause_mode.Reset(PauseMode::CommandDuringPause);
	CHECK(Integration::CurrentPauseStatus() == all);

	/* No simulation tick separates these transitions. */
	_pause_mode = {PauseMode::Normal};
	CHECK(Revision(Integration::CurrentPauseStatus()) == ++revision);
	_pause_mode = {PauseMode::CommandDuringPause};
	Json only_bookkeeping = Integration::CurrentPauseStatus();
	CHECK(Revision(only_bookkeeping) == ++revision);
	CHECK(only_bookkeeping.at("paused") == true);
	CHECK(only_bookkeeping.at("reasons") == Json::array({"other"}));
	_pause_mode = {};
	const Json unpaused = Integration::CurrentPauseStatus();
	CHECK(Revision(unpaused) == ++revision);
	CHECK(unpaused.at("paused") == false);
	CHECK(unpaused.at("reasons").empty());

	static constexpr std::array policies = {std::pair{CommandPauseLevel::NoConstruction, "no_construction"},
		std::pair{CommandPauseLevel::NoLandscaping, "no_landscaping"}, std::pair{CommandPauseLevel::AllActions, "all_actions"}};
	for (const auto &[policy, name] : policies) {
		_settings_game.construction.command_pause_level = policy;
		const Json status = Integration::CurrentPauseStatus();
		CHECK(Revision(status) == ++revision);
		CHECK(status.at("command_policy") == name);
		CHECK(status.at("paused") == false);
		CHECK(Integration::CurrentPauseStatus() == status);
	}
	CHECK(TimerGameTick::counter == tick);
	CHECK(_current_company == company);
	CHECK(Company::GetNumItems() == company_count);
	CheckRandom(random, interactive);
}
