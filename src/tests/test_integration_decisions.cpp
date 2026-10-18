/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file test_integration_decisions.cpp Native decision-information fidelity. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions_internal.h"
#include "../aircraft.h"
#include "../bridge.h"
#include "../cargotype.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../core/endian_func.hpp"
#include "../core/math_func.hpp"
#include "../core/pool_func.hpp"
#include "../core/random_func.hpp"
#include "../depot_base.h"
#include "../economy_func.h"
#include "../engine_base.h"
#include "../engine_func.h"
#include "../language.h"
#include "../newgrf.h"
#include "../newgrf_engine.h"
#include "../newgrf_spritegroup.h"
#include "../newgrf_text.h"
#include "../order_base.h"
#include "../rail.h"
#include "../rail_map.h"
#include "../road.h"
#include "../roadveh.h"
#include "../settings_type.h"
#include "../ship.h"
#include "../station_base.h"
#include "../string_func.h"
#include "../strings_func.h"
#include "../timer/timer_game_calendar.h"
#include "../timer/timer_game_tick.h"
#include "../train.h"
#include "../vehicle_gui.h"
#include "../waypoint_base.h"

#include <filesystem>
#include <fstream>

#include "table/strings.h"
#include "../table/cargo_const.h"

#include "../safeguards.h"

extern RailTypeInfo _railtypes[RAILTYPE_END];
extern RoadTypeInfo _roadtypes[ROADTYPE_END];

namespace {

using Integration::Json;
using Integration::Detail::Handle;
using Integration::Detail::InvalidParameters;
using Integration::Detail::QueryReportsState;
using Integration::Detail::QueryVehicleDetailsState;

struct DecisionWorld {
	CompanyID previous_company = _current_company;
	VehicleSettings previous_vehicle_settings = _settings_game.vehicle;
	EconomySettings previous_economy_settings = _settings_game.economy;
	LandscapeType previous_landscape = _settings_game.game_creation.landscape;
	Randomizer previous_random = _random;
	Randomizer previous_interactive_random = _interactive_random;
	CargoTypes previous_cargo_mask = _cargo_mask;
	CargoTypes previous_standard_cargo_mask = _standard_cargo_mask;
	std::array<CargoSpec, 2> previous_cargo{*CargoSpec::Get(0), *CargoSpec::Get(1)};
	RailTypeInfo previous_rail = *GetRailTypeInfo(RAILTYPE_RAIL);
	RailTypeInfo previous_maglev = *GetRailTypeInfo(RAILTYPE_MAGLEV);
	RoadTypeInfo previous_road = *GetRoadTypeInfo(ROADTYPE_ROAD);
	Company *company = nullptr;
	GRFFile grf{};

	DecisionWorld()
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
		_vehicle_pool.CleanPool();
		_orderlist_pool.CleanPool();
		_engine_pool.CleanPool();
		_depot_pool.CleanPool();
		_station_pool.CleanPool();
		_company_pool.CleanPool();
		_spritegroup_pool.CleanPool();
		Map::Allocate(64, 64);
		_settings_game.game_creation.landscape = LandscapeType::Temperate;
		/* Copy the real built-in passenger/coal specs without rebuilding global
		 * cargo translation or sorted-catalog state. */
		for (size_t i = 0; i < this->previous_cargo.size(); ++i) {
			*CargoSpec::Get(i) = _default_cargo[i];
			CargoSpec::Get(i)->current_payment = CargoSpec::Get(i)->initial_payment;
			_cargo_mask.Set(static_cast<CargoType>(i));
			_standard_cargo_mask.Set(static_cast<CargoType>(i));
		}
		_railtypes[RAILTYPE_RAIL].compatible_railtypes = {RAILTYPE_RAIL};
		_railtypes[RAILTYPE_RAIL].acceleration_type = VehicleAccelerationModel::Normal;
		_railtypes[RAILTYPE_MAGLEV].compatible_railtypes = {RAILTYPE_MAGLEV};
		_railtypes[RAILTYPE_MAGLEV].acceleration_type = VehicleAccelerationModel::Maglev;
		_roadtypes[ROADTYPE_ROAD].powered_roadtypes = {ROADTYPE_ROAD};
		REQUIRE(Company::CanAllocateItem());
		this->company = Company::Create();
		_current_company = this->company->index;
		this->company->money = 1234567;
		this->company->avail_railtypes = {RAILTYPE_RAIL, RAILTYPE_MAGLEV};
		this->company->avail_roadtypes = {ROADTYPE_ROAD};
		this->grf.grf_version = 8;
		this->grf.grfid = GrfID{"\x04\x03\x02\x01"};
	}

	~DecisionWorld()
	{
		_vehicle_pool.CleanPool();
		_orderlist_pool.CleanPool();
		_engine_pool.CleanPool();
		_depot_pool.CleanPool();
		_station_pool.CleanPool();
		_company_pool.CleanPool();
		_spritegroup_pool.CleanPool();
		_railtypes[RAILTYPE_RAIL] = this->previous_rail;
		_railtypes[RAILTYPE_MAGLEV] = this->previous_maglev;
		_roadtypes[ROADTYPE_ROAD] = this->previous_road;
		for (size_t i = 0; i < this->previous_cargo.size(); ++i) *CargoSpec::Get(i) = this->previous_cargo[i];
		_cargo_mask = this->previous_cargo_mask;
		_standard_cargo_mask = this->previous_standard_cargo_mask;
		_settings_game.vehicle = this->previous_vehicle_settings;
		_settings_game.economy = this->previous_economy_settings;
		_settings_game.game_creation.landscape = this->previous_landscape;
		_current_company = this->previous_company;
		_random = this->previous_random;
		_interactive_random = this->previous_interactive_random;
	}

	Engine *NewEngine(VehicleType type)
	{
		REQUIRE(Engine::CanAllocateItem());
		Engine *engine = Engine::Create(type, 0);
		engine->info.climates.Set(LandscapeType::Temperate);
		engine->info.cargo_type = static_cast<CargoType>(0);
		engine->company_avail.Set(this->company->index);
		engine->intro_date = TimerGameCalendar::ConvertYMDToDate(TimerGameCalendar::Year{1943}, 0, 1);
		engine->info.lifelength = TimerGameCalendar::Year{17};
		engine->reliability = 0x6000;
		engine->reliability_max = 0xE000;
		if (type == VehicleType::Train) engine->VehInfo<RailVehicleInfo>().railtypes = {RAILTYPE_RAIL};
		if (type == VehicleType::Road) engine->VehInfo<RoadVehicleInfo>().roadtype = ROADTYPE_ROAD;
		REQUIRE(IsEngineBuildable(engine->index, type, this->company->index));
		return engine;
	}

	template <typename T>
	T *NewVehicle(Engine *engine)
	{
		REQUIRE(Vehicle::CanAllocateItem());
		T *vehicle = T::Create();
		vehicle->owner = this->company->index;
		vehicle->engine_type = engine->index;
		vehicle->tile = TileXY(1, 1);
		vehicle->cargo_type = static_cast<CargoType>(0);
		return vehicle;
	}
};

/** Assert purity before the fixture restores any globals. */
struct ReadState {
	Randomizer random = _random;
	Randomizer interactive_random = _interactive_random;
	CompanyID company_id = _current_company;
	Money money = Company::Get(_current_company)->money;
	Money loan = Company::Get(_current_company)->current_loan;
	TimerGameTick::TickCounter tick = TimerGameTick::counter;
	size_t vehicles = Vehicle::GetNumItems();
	size_t engines = Engine::GetNumItems();
	size_t companies = Company::GetNumItems();
	size_t orders = OrderList::GetNumItems();
	size_t depots = Depot::GetNumItems();
	size_t stations = BaseStation::GetNumItems();

	void Check() const
	{
		CHECK(_current_company == this->company_id);
		CHECK(Company::Get(this->company_id)->money == this->money);
		CHECK(Company::Get(this->company_id)->current_loan == this->loan);
		CHECK(TimerGameTick::counter == this->tick);
		CHECK(Vehicle::GetNumItems() == this->vehicles);
		CHECK(Engine::GetNumItems() == this->engines);
		CHECK(Company::GetNumItems() == this->companies);
		CHECK(OrderList::GetNumItems() == this->orders);
		CHECK(Depot::GetNumItems() == this->depots);
		CHECK(BaseStation::GetNumItems() == this->stations);
		CHECK(_random.state[0] == this->random.state[0]);
		CHECK(_random.state[1] == this->random.state[1]);
		CHECK(_interactive_random.state[0] == this->interactive_random.state[0]);
		CHECK(_interactive_random.state[1] == this->interactive_random.state[1]);
	}
};

Json PurchaseInfo(const Engine *engine)
{
	auto result = QueryVehicleDetailsState("engine.purchase_info", {{"engine_id", engine->index.base()}});
	REQUIRE(result.has_value());
	CHECK(result->at("engine_id") == engine->index.base());
	CHECK(result->at("speed_unit") == "kmh_ish");
	CHECK(result->dump().size() <= 4 * 1024);
	return result->at("performance");
}

void CheckStatus(const Vehicle *vehicle, VehicleStatusKind kind, StringID text)
{
	const auto status = GetVehicleStatusInfo(vehicle);
	CHECK(status.kind == kind);
	CHECK(status.text == GetString(text));
	CHECK(status.text_colour == ExtendedTextColour{TextColour::Black});
}

} // namespace

TEST_CASE("Vehicle status shares native precedence and preserves hover behaviour", "[integration][decision-info][vehicle-status]")
{
	DecisionWorld world;
	Train *train = world.NewVehicle<Train>(world.NewEngine(VehicleType::Train));
	train->subtype |= 1 << GVSF_FRONT;
	train->vehicle_flags.Set(VehicleFlag::PathfinderLost);
	train->vehstatus = {VehState::Crashed, VehState::Stopped};
	train->breakdown_ctr = 1;
	CheckStatus(train, VehicleStatusKind::Crashed, STR_VEHICLE_STATUS_CRASHED);
	train->vehstatus.Reset(VehState::Crashed);
	CheckStatus(train, VehicleStatusKind::BrokenDown, STR_VEHICLE_STATUS_BROKEN_DOWN);
	train->breakdown_ctr = 0;
	train->cur_speed = 12;
	CHECK(GetVehicleStatusInfo(train).kind == VehicleStatusKind::Stopping);
	train->cur_speed = 0;
	CheckStatus(train, VehicleStatusKind::NoPower, STR_VEHICLE_STATUS_TRAIN_NO_POWER);
	train->gcache.cached_power = 100;
	CheckStatus(train, VehicleStatusKind::Stopped, STR_VEHICLE_STATUS_STOPPED);
	const auto hovered = GetVehicleStatusInfo(train, true);
	CHECK(hovered.kind == VehicleStatusKind::NoOrders);
	CHECK(hovered.text_colour == ExtendedTextColour(TextColour::Red, ExtendedTextColourFlag::Forced));
	/* A depot stop retains native stop text even while hovering. */
	REQUIRE(Depot::CanAllocateItem());
	const Depot *depot = Depot::Create(train->tile);
	MakeRailDepot(train->tile, _current_company, depot->index, DiagDirection::NE, RAILTYPE_RAIL);
	train->track = Track::Depot;
	REQUIRE(train->IsStoppedInDepot());
	CheckStatus(train, VehicleStatusKind::Stopped, STR_VEHICLE_STATUS_STOPPED);
	CHECK(GetVehicleStatusInfo(train, true).kind == VehicleStatusKind::Stopped);
	train->track = Track::X;
	train->vehstatus.Reset(VehState::Stopped);
	train->flags.Set(VehicleRailFlag::Stuck);
	CheckStatus(train, VehicleStatusKind::TrainStuck, STR_VEHICLE_STATUS_TRAIN_STUCK);
	train->current_order.MakeLoading(true);
	CheckStatus(train, VehicleStatusKind::LoadingUnloading, STR_VEHICLE_STATUS_LOADING_UNLOADING);
	train->flags.Reset(VehicleRailFlag::Stuck);
	train->current_order.MakeGoToDepot(DepotID::Invalid(), {}, {}, OrderDepotActionFlag::NearestDepot);
	CHECK(GetVehicleStatusInfo(train).kind == VehicleStatusKind::Other);
	CHECK(GetVehicleStatusInfo(train).text.empty());

	RoadVehicle *road = world.NewVehicle<RoadVehicle>(world.NewEngine(VehicleType::Road));
	road->SetFrontEngine();
	road->vehstatus.Set(VehState::Stopped);
	CheckStatus(road, VehicleStatusKind::Stopped, STR_VEHICLE_STATUS_STOPPED);
	road->vehstatus.Reset(VehState::Stopped);
	road->current_order.MakeLeaveStation();
	CheckStatus(road, VehicleStatusKind::Leaving, STR_VEHICLE_STATUS_LEAVING);
	Ship *ship = world.NewVehicle<Ship>(world.NewEngine(VehicleType::Ship));
	ship->breakdown_ctr = 1;
	CheckStatus(ship, VehicleStatusKind::BrokenDown, STR_VEHICLE_STATUS_BROKEN_DOWN);
	Aircraft *aircraft = world.NewVehicle<Aircraft>(world.NewEngine(VehicleType::Aircraft));
	aircraft->subtype = AIR_AIRCRAFT;
	aircraft->breakdown_ctr = 1; // Aircraft ignore the ground-vehicle breakdown selector.
	aircraft->flags.Set(VehicleAirFlag::DestinationTooFar);
	CheckStatus(aircraft, VehicleStatusKind::DestinationTooFar, STR_VEHICLE_STATUS_AIRCRAFT_TOO_FAR);
	aircraft->current_order.MakeLoading(true);
	CheckStatus(aircraft, VehicleStatusKind::LoadingUnloading, STR_VEHICLE_STATUS_LOADING_UNLOADING);

	ReadState state;
	const auto report = QueryVehicleDetailsState("vehicle.details", {{"vehicle_id", Handle(road)}});
	REQUIRE(report.has_value());
	CHECK(report->at("status").at("kind") == "leaving");
	CHECK(report->at("status").at("text") == StrMakeValid(GetVehicleStatusInfo(road).text, StringValidationSetting::AllowNewline));
	CHECK_FALSE(report->at("status").at("text_truncated").get<bool>());
	state.Check();
}

TEST_CASE("Vehicle status reports native lost destinations and bounded text", "[integration][decision-info][vehicle-status]")
{
	DecisionWorld world;
	RoadVehicle *road = world.NewVehicle<RoadVehicle>(world.NewEngine(VehicleType::Road));
	road->SetFrontEngine();
	REQUIRE(Station::CanAllocateItem());
	Station *station = Station::Create(TileXY(2, 2));
	station->name = std::string(1400, 'a');
	station->owner = world.company->index;
	REQUIRE(Waypoint::CanAllocateItem());
	Waypoint *waypoint = Waypoint::Create(TileXY(3, 3));
	waypoint->name = "Test waypoint";
	waypoint->owner = world.company->index;
	REQUIRE(Depot::CanAllocateItem());
	Depot *depot = Depot::Create(TileXY(4, 4));
	depot->name = "Test depot";
	ReadState state;
	road->current_order.MakeGoToStation(station->index);
	CHECK(GetVehicleStatusInfo(road).kind == VehicleStatusKind::HeadingToStation);
	road->vehicle_flags.Set(VehicleFlag::PathfinderLost);
	CHECK(GetVehicleStatusInfo(road).kind == VehicleStatusKind::CannotReachStation);
	const auto report = QueryVehicleDetailsState("vehicle.details", {{"vehicle_id", Handle(road)}});
	REQUIRE(report.has_value());
	CHECK(report->at("status").at("kind") == "cannot_reach_station");
	CHECK(report->at("status").at("text_truncated").get<bool>());
	CHECK(report->at("status").at("text").get<std::string>().size() == 1024);
	CHECK(report->at("status").at("text") == StrMakeValid(GetVehicleStatusInfo(road).text, StringValidationSetting::AllowNewline).substr(0, 1024));
	road->current_order.MakeGoToWaypoint(waypoint->index);
	CHECK(GetVehicleStatusInfo(road).kind == VehicleStatusKind::CannotReachWaypoint);
	CHECK(GetVehicleStatusInfo(road).text.find(waypoint->name) != std::string::npos);
	road->vehicle_flags.Reset(VehicleFlag::PathfinderLost);
	CHECK(GetVehicleStatusInfo(road).kind == VehicleStatusKind::HeadingToWaypoint);
	for (OrderDepotActionFlags action : {OrderDepotActionFlags{OrderDepotActionFlag::Halt}, OrderDepotActionFlags{OrderDepotActionFlag::Unbunch}, OrderDepotActionFlags{}}) {
		road->current_order.MakeGoToDepot(depot->index, OrderDepotTypeFlag::PartOfOrders, {}, action);
		CHECK(GetVehicleStatusInfo(road).kind == VehicleStatusKind::HeadingToDepot);
		const StringID heading = action.Test(OrderDepotActionFlag::Halt) ? STR_VEHICLE_STATUS_HEADING_FOR_DEPOT_VEL :
			action.Test(OrderDepotActionFlag::Unbunch) ? STR_VEHICLE_STATUS_HEADING_FOR_DEPOT_UNBUNCH_VEL : STR_VEHICLE_STATUS_HEADING_FOR_DEPOT_SERVICE_VEL;
		CHECK(GetVehicleStatusInfo(road).text == GetString(heading, road->type, depot->index, PackVelocity(road->GetDisplaySpeed(), road->type)));
		road->vehicle_flags.Set(VehicleFlag::PathfinderLost);
		CHECK(GetVehicleStatusInfo(road).kind == VehicleStatusKind::CannotReachDepot);
		CHECK(GetVehicleStatusInfo(road).text.find(depot->name) != std::string::npos);
		road->vehicle_flags.Reset(VehicleFlag::PathfinderLost);
	}
	state.Check();
}

TEST_CASE("Depot unbunching status follows real shared orders", "[integration][decision-info][vehicle-status]")
{
	DecisionWorld world;
	Engine *engine = world.NewEngine(VehicleType::Road);
	RoadVehicle *road = world.NewVehicle<RoadVehicle>(engine);
	RoadVehicle *peer = world.NewVehicle<RoadVehicle>(engine);
	road->SetFrontEngine();
	peer->SetFrontEngine();
	Order unbunch;
	unbunch.MakeGoToDepot(DepotID::Invalid(), OrderDepotTypeFlag::PartOfOrders, {}, OrderDepotActionFlag::Unbunch);
	Order next;
	next.MakeGoToStation(StationID(0));
	REQUIRE(OrderList::CanAllocateItem());
	road->orders = OrderList::Create(std::vector<Order>{unbunch, next}, road);
	peer->orders = road->orders;
	peer->AddToShared(road);
	road->cur_implicit_order_index = 1;
	road->depot_unbunching_next_departure = TimerGameTick::counter + 100;
	road->state = RVSB_IN_DEPOT;
	ReadState state;
	REQUIRE(road->IsWaitingForUnbunching());
	CheckStatus(road, VehicleStatusKind::WaitingUnbunching, STR_VEHICLE_STATUS_WAITING_UNBUNCHING);
	road->vehstatus.Set(VehState::Stopped);
	CheckStatus(road, VehicleStatusKind::Stopped, STR_VEHICLE_STATUS_STOPPED);
	state.Check();
}

TEST_CASE("Cargo payment samples are native graph values under time and aging settings", "[integration][decision-info][cargo-payment]")
{
	DecisionWorld world;
	ReadState state;
	for (CargoType cargo : {static_cast<CargoType>(0), static_cast<CargoType>(1)}) {
		for (TimekeepingUnits units : {TimekeepingUnits::Calendar, TimekeepingUnits::Wallclock}) {
			_settings_game.economy.timekeeping_units = units;
			for (uint16_t aging : {uint16_t{50}, uint16_t{175}}) {
				_settings_game.economy.cargo_aging_rate = aging;
				/* Current payment is the native already-inflated rate. */
				CargoSpec::Get(cargo)->current_payment = CargoSpec::Get(cargo)->initial_payment * 3;
				const auto result = QueryReportsState("cargo.payment_rates", {{"cargo_id", to_underlying(cargo)}});
				REQUIRE(result.has_value());
				CHECK(result->at("amount") == 10);
				CHECK(result->at("distance_tiles") == 20);
				CHECK(result->at("money_unit") == "native_money");
				CHECK(result->at("income_scope") == "base_delivery");
				CHECK(result->at("time_unit") == (units == TimekeepingUnits::Wallclock ? "wallclock_seconds" : "economy_days"));
				REQUIRE(result->at("samples").size() == 20);
				for (size_t i = 0; i < 20; ++i) {
					const uint16_t periods = static_cast<uint16_t>((i + 1) * 4);
					const Json &sample = result->at("samples").at(i);
					CHECK(sample.at("transit_periods") == periods);
					CHECK(sample.at("display_time") == (i + 1) * (units == TimekeepingUnits::Wallclock ? 20 : 10));
					CHECK(sample.at("income") == GetTransportedGoodsIncome(10, 20, periods, cargo).base());
				}
				CHECK(result->dump().size() <= 8 * 1024);
			}
		}
	}
	CHECK_THROWS_AS(QueryReportsState("cargo.payment_rates", {{"cargo_id", 64}}), InvalidParameters);
	CargoSpec::Get(1)->bitnum = INVALID_CARGO_BITNUM;
	CHECK_THROWS_AS(QueryReportsState("cargo.payment_rates", {{"cargo_id", 1}}), InvalidParameters);
	CargoSpec::Get(1)->bitnum = 1;
	CargoSpec::Get(1)->classes.Set(CargoClass::Special);
	_standard_cargo_mask.Reset(static_cast<CargoType>(1));
	CHECK_THROWS_AS(QueryReportsState("cargo.payment_rates", {{"cargo_id", 1}}), InvalidParameters);
	CHECK_THROWS_AS(QueryReportsState("cargo.payment_rates", {{"cargo_id", 0}, {"amount", 1}}), InvalidParameters);
	state.Check();
}

TEST_CASE("Cargo payment preserves signed native profit callback income", "[integration][decision-info][cargo-payment]")
{
	DecisionWorld world;
	REQUIRE(CallbackResultSpriteGroup::CanAllocateItem());
	CargoSpec *cargo = CargoSpec::Get(0);
	cargo->group = CallbackResultSpriteGroup::Create(0x7FFF); // Native signed -1 multiplier.
	cargo->grffile = &world.grf;
	cargo->callback_mask.Set(CargoCallbackMask::ProfitCalc);
	cargo->current_payment = 8192;
	ReadState state;
	REQUIRE(GetTransportedGoodsIncome(10, 20, 4, static_cast<CargoType>(0)).base() == -10);
	const auto result = QueryReportsState("cargo.payment_rates", {{"cargo_id", 0}});
	REQUIRE(result.has_value());
	for (const Json &sample : result->at("samples")) {
		CHECK(sample.at("income").get<int64_t>() == -10);
	}
	state.Check();
}

TEST_CASE("Bridge catalog uses native availability, span clamps and speed sentinels", "[integration][decision-info][bridges]")
{
	DecisionWorld world;
	struct RestoreBridges {
		std::array<BridgeSpec, MAX_BRIDGES> bridges;
		TimerGameCalendar::Year year = TimerGameCalendar::year;
		uint16_t maximum = _settings_game.construction.max_bridge_length;
		RestoreBridges() { std::copy(std::begin(_bridge), std::end(_bridge), this->bridges.begin()); }
		~RestoreBridges()
		{
			std::copy(this->bridges.begin(), this->bridges.end(), std::begin(_bridge));
			TimerGameCalendar::year = this->year;
			_settings_game.construction.max_bridge_length = this->maximum;
			CleanUpStrings();
		}
	} restore;
	TimerGameCalendar::year = TimerGameCalendar::Year{1950};
	_settings_game.construction.max_bridge_length = 8;
	for (BridgeSpec &bridge : _bridge) {
		bridge.avail_year = TimerGameCalendar::Year{1900};
		bridge.min_length = 0;
		bridge.max_length = 16;
		bridge.speed = 120;
		bridge.material = STR_BRIDGE_NAME_WOODEN;
	}
	_bridge[0].speed = 0;
	_bridge[1].speed = UINT16_MAX;
	_bridge[2].avail_year = TimerGameCalendar::Year{1951};
	_bridge[3].min_length = 9;
	_bridge[4].min_length = 4;
	_bridge[4].max_length = 3;
	ReadState state;
	const auto result = QueryReportsState("catalog.bridges", Json::object());
	REQUIRE(result.has_value());
	CHECK(result->at("transports") == Json{"rail", "road"});
	CHECK(result->at("span_unit") == "interior_tiles_excluding_heads");
	CHECK(result->at("speed_unit") == "kmh_ish");
	REQUIRE(result->at("items").size() == MAX_BRIDGES - 3);
	CHECK(result->at("items").at(0).at("minimum_span_tiles") == 0);
	CHECK(result->at("items").at(0).at("maximum_span_tiles") == 8);
	CHECK(result->at("items").at(0).at("maximum_speed") == 0);
	CHECK(result->at("items").at(1).at("maximum_speed").is_null());
	uint previous = 0;
	for (const Json &item : result->at("items")) {
		const auto type = item.at("bridge_type").get<BridgeType>();
		CHECK(type >= previous);
		previous = type;
		CHECK(CheckBridgeAvailability(type, GetBridgeSpec(type)->min_length, {}).Succeeded());
		CHECK(item.at("material") == GetString(STR_BRIDGE_NAME_WOODEN));
		CHECK_FALSE(item.at("material_truncated").get<bool>());
	}
	CHECK(result->dump().size() <= 24 * 1024);
	_bridge[2].avail_year = TimerGameCalendar::Year{1900};
	_bridge[3].min_length = 0;
	_bridge[4].min_length = 0;
	const auto all = QueryReportsState("catalog.bridges", Json::object());
	REQUIRE(all.has_value());
	CHECK(all->at("items").size() == MAX_BRIDGES);
	/* The multibyte character straddles byte512 and must be omitted whole. */
	const std::string material = std::string(511, 'b') + "\xC3\xA9" + " tail";
	_bridge[0].material = AddGRFString(world.grf.grfid, GRFSTR_MISC_GRF_TEXT, static_cast<uint8_t>(GRFLanguage::English), true, true, material, STR_UNDEFINED);
	const auto bounded = QueryReportsState("catalog.bridges", Json::object());
	REQUIRE(bounded.has_value());
	CHECK(bounded->at("items").at(0).at("material") == std::string(511, 'b'));
	CHECK(bounded->at("items").at(0).at("material_truncated").get<bool>());
	CHECK_THROWS_AS(QueryReportsState("catalog.bridges", {{"transport", "road"}}), InvalidParameters);
	state.Check();
}

TEST_CASE("Purchase performance follows native getters, eligibility and applicability", "[integration][decision-info][engine-purchase]")
{
	DecisionWorld world;
	_settings_game.vehicle.extend_vehicle_life = 5;
	_settings_game.vehicle.train_acceleration_model = AccelerationModel::Realistic;
	_settings_game.vehicle.roadveh_acceleration_model = AccelerationModel::Realistic;
	Engine *train = world.NewEngine(VehicleType::Train);
	RailVehicleInfo &rail = train->VehInfo<RailVehicleInfo>();
	rail.railveh_type = RailVehicleType::Multihead;
	rail.weight = 33;
	rail.power = 1350;
	rail.tractive_effort = 128;
	rail.pow_wag_power = 200;
	rail.pow_wag_weight = 9;
	train->company_hidden.Set(world.company->index);
	Engine *road = world.NewEngine(VehicleType::Road);
	Engine *ship = world.NewEngine(VehicleType::Ship);
	Engine *aircraft = world.NewEngine(VehicleType::Aircraft);
	ReadState state;
	Json performance = PurchaseInfo(train);
	CHECK(performance.at("designed_year") == 1943);
	CHECK(performance.at("life_calendar_days") == train->GetLifeLengthInDays().base());
	CHECK(performance.at("reliability_percent") == ToPercent16(train->reliability));
	CHECK(performance.at("reliability_percent") != ToPercent16(train->reliability_max));
	CHECK(performance.at("empty_weight_tonnes") == 66);
	CHECK(performance.at("power_hp") == train->GetPower());
	CHECK(performance.at("maximum_tractive_effort_newtons") == train->GetDisplayMaxTractiveEffort());
	CHECK(performance.at("maximum_tractive_effort_newtons").get<uint>() > 1000);
	CHECK(performance.at("powered_wagon_power_hp") == 200);
	CHECK(performance.at("powered_wagon_extra_weight_tonnes") == 9);
	rail.pow_wag_power = 0;
	performance = PurchaseInfo(train);
	CHECK_FALSE(performance.contains("powered_wagon_power_hp"));
	CHECK_FALSE(performance.contains("powered_wagon_extra_weight_tonnes"));
	rail.pow_wag_power = 200;
	_settings_game.vehicle.train_acceleration_model = AccelerationModel::Original;
	CHECK_FALSE(PurchaseInfo(train).contains("maximum_tractive_effort_newtons"));
	_settings_game.vehicle.train_acceleration_model = AccelerationModel::Realistic;
	rail.railtypes = {RAILTYPE_MAGLEV};
	CHECK_FALSE(PurchaseInfo(train).contains("maximum_tractive_effort_newtons"));
	rail.railtypes.Set(RAILTYPE_RAIL);
	CHECK(PurchaseInfo(train).contains("maximum_tractive_effort_newtons"));
	rail.railveh_type = RailVehicleType::Wagon;
	performance = PurchaseInfo(train);
	CHECK(performance.at("empty_weight_tonnes") == train->GetDisplayWeight());
	for (const char *field : {"designed_year", "life_calendar_days", "reliability_percent", "power_hp", "maximum_tractive_effort_newtons", "powered_wagon_power_hp", "powered_wagon_extra_weight_tonnes"}) {
		CHECK_FALSE(performance.contains(field));
	}
	performance = PurchaseInfo(road);
	CHECK(performance.at("empty_weight_tonnes") == road->GetDisplayWeight());
	CHECK(performance.at("power_hp") == road->GetPower());
	CHECK(performance.at("maximum_tractive_effort_newtons") == road->GetDisplayMaxTractiveEffort());
	_settings_game.vehicle.roadveh_acceleration_model = AccelerationModel::Original;
	performance = PurchaseInfo(road);
	for (const char *field : {"empty_weight_tonnes", "power_hp", "maximum_tractive_effort_newtons"}) CHECK_FALSE(performance.contains(field));
	ShipVehicleInfo &water = ship->VehInfo<ShipVehicleInfo>();
	water.max_speed = 128;
	water.ocean_speed_frac = 64;
	water.canal_speed_frac = 192;
	performance = PurchaseInfo(ship);
	CHECK(performance.at("ocean_speed") == water.ApplyWaterClassSpeedFrac(ship->GetDisplayMaxSpeed(), true));
	CHECK(performance.at("canal_speed") == water.ApplyWaterClassSpeedFrac(ship->GetDisplayMaxSpeed(), false));
	CHECK(performance.at("ocean_speed") != performance.at("canal_speed"));
	for (Engine *nonground : {ship, aircraft}) {
		performance = PurchaseInfo(nonground); // Ground-only getters assert for these real model types.
		for (const char *field : {"empty_weight_tonnes", "power_hp", "maximum_tractive_effort_newtons"}) CHECK_FALSE(performance.contains(field));
	}
	road->company_avail.Reset(world.company->index);
	CHECK_FALSE(IsEngineBuildable(road->index, road->type, world.company->index));
	CHECK_THROWS_AS(PurchaseInfo(road), InvalidParameters);
	road->company_avail.Set(world.company->index);
	road->info.climates.Reset(LandscapeType::Temperate);
	CHECK_THROWS_AS(PurchaseInfo(road), InvalidParameters);
	CHECK_THROWS_AS(QueryVehicleDetailsState("engine.purchase_info", {{"engine_id", 63999}}), InvalidParameters);
	CHECK_THROWS_AS(QueryVehicleDetailsState("engine.purchase_info", {{"engine_id", train->index.base()}, {"company_id", 0}}), InvalidParameters);
	state.Check();
}

TEST_CASE("Purchase performance evaluates the native property callback", "[integration][decision-info][engine-purchase]")
{
	DecisionWorld world;
	_settings_game.vehicle.roadveh_acceleration_model = AccelerationModel::Realistic;
	Engine *engine = world.NewEngine(VehicleType::Road);
	SetEngineGRF(engine->index, &world.grf);
	REQUIRE(CallbackResultSpriteGroup::CanAllocateItem());
	engine->grf_prop.SetSpriteGroup(CargoGRFFileProps::SG_DEFAULT, CallbackResultSpriteGroup::Create(28));
	ReadState state;
	REQUIRE(engine->GetPower() == 280);
	REQUIRE(engine->GetDisplayWeight() == 7);
	const Json performance = PurchaseInfo(engine);
	CHECK(performance.at("power_hp") == engine->GetPower());
	CHECK(performance.at("empty_weight_tonnes") == engine->GetDisplayWeight());
	CHECK(performance.at("maximum_tractive_effort_newtons") == engine->GetDisplayMaxTractiveEffort());
	state.Check();
}

TEST_CASE("Decision queries publish precise schemas and keep current-company eligibility", "[integration][decision-info][capabilities]")
{
	DecisionWorld world;
	Engine *engine = world.NewEngine(VehicleType::Ship);
	REQUIRE(Company::CanAllocateItem());
	Company *other = Company::Create();
	ReadState state;
	const Json capabilities = Integration::CapabilityMatrix();
	const Json &queries = capabilities.at("queries");
	auto query_spec = [&](std::string_view name) -> const Json & {
		const auto found = std::ranges::find_if(queries, [&](const Json &spec) { return spec.at("name") == name; });
		REQUIRE(found != queries.end());
		return *found;
	};
	for (const char *name : {"cargo.payment_rates", "catalog.bridges", "engine.purchase_info"}) {
		const Json &spec = query_spec(name);
		CHECK(spec.at("modes") == Json::array({"text"}));
		CHECK(spec.at("parameters").at("additionalProperties") == false);
	}
	CHECK(query_spec("cargo.payment_rates").at("parameters").at("required") == Json::array({"cargo_id"}));
	CHECK(query_spec("cargo.payment_rates").at("parameters").at("properties").at("cargo_id").at("maximum") == 63);
	CHECK(query_spec("catalog.bridges").at("parameters").at("required").empty());
	CHECK(query_spec("engine.purchase_info").at("parameters").at("required") == Json::array({"engine_id"}));
	const Json &vehicle_fields = query_spec("vehicle.details").at("result_fields");
	CHECK(std::ranges::find(vehicle_fields, "status") != vehicle_fields.end());
	CHECK(Integration::QueryState("cargo.payment_rates", {{"cargo_id", 0}}).contains("samples"));
	CHECK(Integration::QueryState("engine.purchase_info", {{"engine_id", engine->index.base()}}).contains("performance"));
	for (const auto &[name, parameters] : std::vector<std::pair<std::string, Json>>{
		{"cargo.payment_rates", {{"cargo_id", 0}, {"amount", 1}}},
		{"cargo.payment_rates", {{"cargo_id", true}}},
		{"catalog.bridges", {{"transport", "road"}}},
		{"engine.purchase_info", {{"engine_id", engine->index.base()}, {"company_id", other->index.base()}}},
	}) {
		CHECK(Integration::QueryState(name, parameters).at("error").at("code") == "invalid_parameters");
	}
	_current_company = other->index;
	CHECK_FALSE(IsEngineBuildable(engine->index, engine->type, _current_company));
	CHECK(Integration::QueryState("engine.purchase_info", {{"engine_id", engine->index.base()}}).at("error").at("code") == "invalid_parameters");
	_current_company = world.company->index;
	state.Check();
}
