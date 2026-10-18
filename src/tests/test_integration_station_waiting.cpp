/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file test_integration_station_waiting.cpp Native station packet histogram observations. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions_internal.h"
#include "../cargotype.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../settings_type.h"
#include "../station_base.h"
#include "../station_cmd.h"

#include <chrono>
#include <thread>

#include "../safeguards.h"

namespace {

using Integration::Json;
using namespace Integration::Detail;

struct WaitingWorld {
	CompanyID previous_company = _current_company;
	DistributionType previous_distribution = _settings_game.linkgraph.distribution_pax;
	LandscapeType previous_landscape = _settings_game.game_creation.landscape;
	Station *station = nullptr;
	CargoType cargo = static_cast<CargoType>(0);

	WaitingWorld()
	{
		_station_pool.CleanPool();
		_cargopacket_pool.CleanPool();
		_company_pool.CleanPool();
		Map::Allocate(64, 64);
		SetupCargoForClimate(LandscapeType::Temperate);
		REQUIRE(CargoSpec::Get(this->cargo)->IsValid());
		_settings_game.linkgraph.distribution_pax = DistributionType::Asymmetric;
		REQUIRE(Company::CanAllocateItem());
		_current_company = Company::Create()->index;
		REQUIRE(Station::CanAllocateItem());
		this->station = Station::Create(TileXY(20, 20));
		this->station->owner = _current_company;
	}

	~WaitingWorld()
	{
		_station_pool.CleanPool();
		_cargopacket_pool.CleanPool();
		_company_pool.CleanPool();
		_current_company = this->previous_company;
		_settings_game.linkgraph.distribution_pax = this->previous_distribution;
		SetupCargoForClimate(this->previous_landscape);
	}

	void Append(uint16_t units, StationID next, SourceID source_id = 0)
	{
		REQUIRE(CargoPacket::CanAllocateItem());
		CargoPacket *packet = CargoPacket::Create(this->station->index, units, Source(source_id, SourceType::Town));
		this->station->goods[this->cargo].GetOrCreateData().cargo.Append(packet, next);
	}

	Json Read(Json extra = Json::object()) const
	{
		/* The production gate is global, including between independent test cases. */
		std::this_thread::sleep_for(std::chrono::milliseconds(1050));
		Json parameters = {{"station_id", Handle(this->station)}, {"cargo_id", to_underlying(this->cargo)}};
		parameters.update(extra);
		auto result = QueryStationDetailsState("station.cargo.waiting", parameters);
		REQUIRE(result.has_value());
		return *result;
	}
};

} // namespace

TEST_CASE("Station waiting histogram traverses more than the former packet and group limits", "[integration][station_waiting]")
{
	WaitingWorld world;
	constexpr uint32_t PACKETS = 8193;
	for (uint32_t i = 0; i < PACKETS; ++i) world.Append(CargoPacket::MAX_COUNT, StationID(i + 1));
	const Json first = world.Read({{"limit", 64}});
	CHECK(first.at("available_units") == static_cast<uint64_t>(PACKETS) * CargoPacket::MAX_COUNT);
	CHECK(first.at("native_available_cached") == first.at("available_units"));
	CHECK(first.at("available_matches_native_cache") == true);
	CHECK(first.at("native_total_addition_fits_u32") == true);
	CHECK(first.at("rows").size() == 64);
	CHECK(first.at("complete") == false);
	CHECK(first.at("live") == true);
	CHECK(first.at("destination_semantics") == "not_estimated");
	CHECK(first.at("aggregate_version").get<std::string>().size() == 69);
}

TEST_CASE("Station waiting histogram versions per-packet sizes and offers live or strict continuation", "[integration][station_waiting]")
{
	WaitingWorld world;
	world.Append(1, StationID(10), 1);
	world.Append(1, StationID(10), 2);
	world.Append(2, StationID(10), 3);
	world.Append(3, StationID(11), 4);
	const Json first = world.Read({{"limit", 1}});
	REQUIRE(first.at("rows").size() == 1);
	CHECK(first.at("available_units") == 7);
	CHECK(first.at("rows").at(0).at("packet_units") == 1);
	CHECK(first.at("rows").at(0).at("packet_count") == 2);
	CHECK(first.at("rows").at(0).at("units") == 2);
	const Json second = world.Read({{"limit", 1}, {"after_group", first.at("next_after_group")}});
	REQUIRE(second.at("rows").size() == 1);
	CHECK(second.at("rows").at(0).at("packet_units") == 2);
	const Json third = world.Read({{"limit", 1}, {"after_group", second.at("next_after_group")},
		{"aggregate_version", first.at("aggregate_version")}});
	REQUIRE(third.at("rows").size() == 1);
	CHECK(third.at("rows").at(0).at("packet_units") == 3);
	CHECK(third.at("complete") == true);

	/* A missing prior key remains a valid live lower bound; a strict version detects changed cargo. */
	world.Append(4, StationID(12), 5);
	const Json live = world.Read({{"limit", 1}, {"after_group", Json{{"first_station_id", world.station->index.base()},
		{"next_hop_id", 11}, {"packet_units", 2}}}});
	REQUIRE(live.at("rows").size() == 1);
	CHECK(live.at("rows").at(0).at("packet_units") == 3);
	std::this_thread::sleep_for(std::chrono::milliseconds(1050));
	const Json strict_parameters = {{"station_id", Handle(world.station)}, {"cargo_id", to_underlying(world.cargo)},
		{"after_group", first.at("next_after_group")}, {"aggregate_version", first.at("aggregate_version")}};
	CHECK_THROWS_AS(QueryStationDetailsState("station.cargo.waiting", strict_parameters), InvalidParameters);
}

TEST_CASE("Station waiting manual mode suppresses next hop while retaining factual source", "[integration][station_waiting]")
{
	WaitingWorld world;
	_settings_game.linkgraph.distribution_pax = DistributionType::Manual;
	world.Append(2, StationID(10), 1);
	world.Append(2, StationID(11), 2);
	const Json result = world.Read();
	REQUIRE(result.at("rows").size() == 1);
	CHECK(result.at("distribution_mode") == "manual");
	CHECK(result.at("route_details_suppressed") == true);
	CHECK(result.at("rows").at(0).at("first_station_id") == world.station->index.base());
	CHECK(result.at("rows").at(0).at("source_visible_in_gui") == false);
	CHECK(result.at("rows").at(0).at("next_hop_id").is_null());
	CHECK(result.at("rows").at(0).at("next_hop").is_null());
	CHECK(result.at("rows").at(0).at("packet_count") == 2);
	CHECK(result.at("rows").at(0).at("units") == 4);
	CHECK(result.at("next_after_group").is_null());
}

TEST_CASE("Station waiting keeps complete uint64 units when native cached available count wraps", "[integration][station_waiting]")
{
	WaitingWorld world;
	constexpr uint32_t PACKETS = 65538;
	for (uint32_t i = 0; i < PACKETS; ++i) world.Append(CargoPacket::MAX_COUNT, StationID(1 + i % 60000));
	const Json result = world.Read({{"limit", 1}});
	const uint64_t exact = static_cast<uint64_t>(PACKETS) * CargoPacket::MAX_COUNT;
	REQUIRE(exact > UINT32_MAX);
	CHECK(result.at("available_units") == exact);
	CHECK(result.at("native_available_cached") == static_cast<uint32_t>(exact));
	CHECK(result.at("available_matches_native_cache") == false);
	CHECK(result.at("native_reserved_cached") == 0);
	CHECK(result.at("rows").size() == 1);
	CHECK(result.at("complete") == false);
}

TEST_CASE("Station flow pages reveal data presence separately from empty flows", "[integration][station_waiting]")
{
	WaitingWorld world;
	const Json parameters = {{"station_id", Handle(world.station)}, {"cargo_id", to_underlying(world.cargo)}};
	const Json without_data = *QueryStationDetailsState("station.cargo.flows", parameters);
	CHECK(without_data.at("rows").empty());
	CHECK(without_data.at("has_cargo_data") == false);
	CHECK(without_data.at("distribution_mode") == "asymmetric");
	world.station->goods[world.cargo].GetOrCreateData();
	const Json with_data = *QueryStationDetailsState("station.cargo.flows", parameters);
	CHECK(with_data.at("rows").empty());
	CHECK(with_data.at("has_cargo_data") == true);
}

TEST_CASE("Station waiting validates handles before its process-global rate gate", "[integration][station_waiting]")
{
	WaitingWorld world;
	world.Append(1, StationID(10));
	std::this_thread::sleep_for(std::chrono::milliseconds(1050));
	const Json valid = {{"station_id", Handle(world.station)}, {"cargo_id", to_underlying(world.cargo)}};
	Json stale = valid;
	stale["station_id"]["generation"] = world.station->GetInstanceGeneration() + 1;
	CHECK_THROWS_AS(QueryStationDetailsState("station.cargo.waiting", stale), InvalidParameters);
	const auto admitted = QueryStationDetailsState("station.cargo.waiting", valid);
	REQUIRE(admitted.has_value());
	CHECK(admitted->at("available_units") == 1);
	CHECK_THROWS_AS(QueryStationDetailsState("station.cargo.waiting", valid), InvalidParameters);
}
