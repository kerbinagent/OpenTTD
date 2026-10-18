/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file test_integration_company_settings_registry.cpp Exact native company-setting aliases. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions_internal.h"
#include "../command_func.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../core/pool_func.hpp"
#include "../misc/endian_buffer.hpp"
#include "../order_func.h"
#include "../settings_cmd.h"
#include "../settings_internal.h"
#include "../integration/bridge_hooks.h"

#include "../safeguards.h"

namespace {

using Integration::Json;
using Integration::Detail::InvalidParameters;
using Integration::Detail::ManagementIntentState;
using Integration::Detail::IsAllowedCompanySetting;

struct SettingWorld {
	CompanyID previous_company = _current_company;
	Company *company;

	SettingWorld()
	{
		_company_pool.CleanPool();
		Map::Allocate(64, 64);
		REQUIRE(Company::CanAllocateItem());
		this->company = Company::Create();
		_current_company = this->company->index;
	}
	~SettingWorld()
	{
		_company_pool.CleanPool();
		_current_company = this->previous_company;
	}
};

CommandDataBuffer NativeSetting(std::string name, int32_t value)
{
	using Args = CommandTraits<Commands::ChangeCompanySetting>::Args;
	return EndianBufferWriter<CommandDataBuffer>::FromValue(Args{std::move(name), value});
}

std::optional<Json> Proof(std::string name, int32_t value)
{
	return ManagementIntentState(Commands::ChangeCompanySetting, NativeSetting(std::move(name), value));
}

TEST_CASE("nine company settings admit only exact bare and canonical aliases", "[integration_company_settings_registry]")
{
	SettingWorld world;
	REQUIRE_FALSE(Integration::RendererIsActive()); // Authoritative native-proof fixture.
	const bool original_renew = world.company->settings.engine_renew;
	struct Case { const char *canonical; const char *bare; int32_t value; };
	static constexpr std::array<Case, 9> cases = {{
		{"company.engine_renew", "engine_renew", 1},
		{"company.engine_renew_months", "engine_renew_months", 6},
		{"company.engine_renew_money", "engine_renew_money", 100000},
		{"company.renew_keep_length", "renew_keep_length", 1},
		{"company.vehicle.servint_ispercent", "vehicle.servint_ispercent", 0},
		{"company.vehicle.servint_trains", "vehicle.servint_trains", 0},
		{"company.vehicle.servint_roadveh", "vehicle.servint_roadveh", 0},
		{"company.vehicle.servint_ships", "vehicle.servint_ships", 0},
		{"company.vehicle.servint_aircraft", "vehicle.servint_aircraft", 0},
	}};
	for (const Case &entry : cases) {
		INFO(entry.canonical);
		const SettingDesc *descriptor = GetSettingFromName(entry.canonical);
		REQUIRE(descriptor != nullptr);
		REQUIRE(IsAllowedCompanySetting(descriptor));
		const auto canonical = Proof(entry.canonical, entry.value);
		const auto bare = Proof(entry.bare, entry.value);
		REQUIRE(canonical.has_value());
		REQUIRE(bare == canonical);
	}
	REQUIRE_FALSE(IsAllowedCompanySetting(nullptr));
	const SettingDesc *unrelated = GetSettingFromName("gui.starting_colour");
	REQUIRE(unrelated != nullptr);
	REQUIRE_FALSE(IsAllowedCompanySetting(unrelated));
	IntSettingDesc copied = *GetSettingFromName("company.engine_renew_months")->AsIntSetting();
	REQUIRE_FALSE(IsAllowedCompanySetting(&copied)); // Exact descriptor identity, not just the same name.
	for (const char *name : {"company.company.engine_renew", "Engine_renew", "engine_renew_extra", "company.vehicle.servint_trains.extra", "gui.starting_colour", "vehicle.servint_unknown"}) {
		INFO(name);
		REQUIRE_THROWS_AS(Proof(name, 1), InvalidParameters);
	}
	REQUIRE_THROWS_AS(Proof("engine_renew", 2), InvalidParameters);
	REQUIRE_THROWS_AS(Proof("company.engine_renew", -1), InvalidParameters);
	REQUIRE_THROWS_AS(Proof("vehicle.servint_ispercent", 2), InvalidParameters);
	REQUIRE_THROWS_AS(Proof("engine_renew_months", -13), InvalidParameters);
	REQUIRE_THROWS_AS(Proof("company.engine_renew_months", 13), InvalidParameters);
	REQUIRE_THROWS_AS(Proof("engine_renew_money", 2000001), InvalidParameters);
	REQUIRE_THROWS_AS(Proof("vehicle.servint_trains", 801), InvalidParameters);
	REQUIRE_THROWS_AS(Proof("vehicle.servint_trains", -1), InvalidParameters);
	const bool original_percent = world.company->settings.vehicle.servint_ispercent;
	world.company->settings.vehicle.servint_ispercent = false;
	const int32_t minimum_time = GetServiceIntervalClamped(1, false); // Current game day/minute mode.
	const int32_t maximum_time = GetServiceIntervalClamped(800, false);
	REQUIRE(Proof("vehicle.servint_trains", 0).has_value());
	REQUIRE(Proof("vehicle.servint_trains", minimum_time).has_value());
	REQUIRE(Proof("vehicle.servint_trains", maximum_time).has_value());
	if (minimum_time > 1) REQUIRE_THROWS_AS(Proof("vehicle.servint_trains", minimum_time - 1), InvalidParameters);
	REQUIRE_THROWS_AS(Proof("vehicle.servint_trains", maximum_time + 1), InvalidParameters);
	world.company->settings.vehicle.servint_ispercent = true;
	REQUIRE(Proof("vehicle.servint_trains", 0).has_value());
	REQUIRE(Proof("vehicle.servint_trains", 5).has_value());
	REQUIRE(Proof("vehicle.servint_trains", 90).has_value());
	REQUIRE_THROWS_AS(Proof("vehicle.servint_trains", 4), InvalidParameters);
	REQUIRE_THROWS_AS(Proof("vehicle.servint_trains", 91), InvalidParameters);
	world.company->settings.vehicle.servint_ispercent = original_percent;
	REQUIRE(world.company->settings.engine_renew == original_renew); // Proof never submits the command.
}

} // namespace
