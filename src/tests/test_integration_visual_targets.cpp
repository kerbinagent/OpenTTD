/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file test_integration_visual_targets.cpp Native waypoint reuse and screenshot target provenance. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../misc/endian_buffer.hpp"
#include "../core/pool_func.hpp"
#include "../newgrf_station.h"
#include "../rail_map.h"
#include "../settings_type.h"
#include "../waypoint_base.h"
#include "../waypoint_cmd.h"
#include "table/strings.h"

#include "../safeguards.h"

namespace {

/** A small real native world; no GUI, mocked selectors or alternate command implementation. */
struct NativeWaypointWorld {
	CompanyID previous_company = _current_company;
	uint8_t previous_spread = _settings_game.station.station_spread;
	TileIndex start;

	NativeWaypointWorld()
	{
		_station_pool.CleanPool();
		_company_pool.CleanPool();
		Map::Allocate(64, 64);
		StationClass::Reset();
		REQUIRE(Company::CanAllocateItem());
		auto *company = Company::Create();
		_current_company = company->index;
		_settings_game.station.station_spread = 16;
		this->start = TileXY(16, 20);
		for (uint y = 20; y < 32; ++y) MakeRailNormal(TileXY(16, y), company->index, TrackBits{Track::X}, RAILTYPE_RAIL);
	}

	~NativeWaypointWorld()
	{
		_station_pool.CleanPool();
		_company_pool.CleanPool();
		_current_company = this->previous_company;
		_settings_game.station.station_spread = this->previous_spread;
	}

	Waypoint *GrayWaypoint(TileIndex tile) const
	{
		REQUIRE(Waypoint::CanAllocateItem());
		auto *waypoint = Waypoint::Create(tile);
		waypoint->owner = _current_company;
		waypoint->string_id = STR_SV_STNAME_WAYPOINT;
		REQUIRE_FALSE(waypoint->IsInUse());
		return waypoint;
	}

	CommandDataBuffer Intent() const
	{
		using Args = CommandTraits<Commands::BuildRailWaypoint>::Args;
		return EndianBufferWriter<CommandDataBuffer>::FromValue(Args{this->start, Axis::X, uint8_t{1}, uint8_t{12}, STAT_CLASS_WAYP, uint16_t{0}, StationID::Invalid(), true});
	}

	CommandCost NativeQuery() const
	{
		return CmdBuildRailWaypoint({}, this->start, Axis::X, 1, 12, STAT_CLASS_WAYP, 0, StationID::Invalid(), true);
	}
};

} // namespace

TEST_CASE("Visual native waypoint reuse rejects gray identities created after the screenshot", "[integration][visual]")
{
	NativeWaypointWorld world;
	const Integration::TargetWatermarks screenshot{0, BaseStation::GetLastInstanceGeneration(), 0, Company::GetLastInstanceGeneration()};
	const auto data = world.Intent();
	/* The native center is (16,26). This gray sign is within its reuse radius,
	 * but ten tiles from the northern origin, outside the former wrong scan. */
	const Waypoint *created_after = world.GrayWaypoint(TileXY(18, 28));
	REQUIRE(created_after->GetInstanceGeneration() > screenshot.station);
	REQUIRE(world.NativeQuery().Succeeded());
	CHECK_FALSE(Integration::InspectNativeTargets(Commands::BuildRailWaypoint, data, screenshot).accepted);
	const Integration::TargetWatermarks fresh{0, BaseStation::GetLastInstanceGeneration(), 0, Company::GetLastInstanceGeneration()};
	CHECK(Integration::InspectNativeTargets(Commands::BuildRailWaypoint, data, fresh).accepted);
}

TEST_CASE("Visual native reuse proof changes when an existing gray sign moves", "[integration][visual]")
{
	NativeWaypointWorld world;
	Waypoint *waypoint = world.GrayWaypoint(TileXY(18, 28));
	const auto data = world.Intent();
	REQUIRE(world.NativeQuery().Succeeded());
	const auto before = Integration::NativeIntentState(Commands::BuildRailWaypoint, data);
	/* Reusing then removing a waypoint can move its gray sign without creating
	 * a new pool incarnation. That can change the native closest-sign choice. */
	const auto generation = waypoint->GetInstanceGeneration();
	waypoint->xy = TileXY(19, 28);
	REQUIRE(world.NativeQuery().Succeeded());
	CHECK(waypoint->GetInstanceGeneration() == generation);
	CHECK(Integration::NativeIntentState(Commands::BuildRailWaypoint, data) != before);
}
