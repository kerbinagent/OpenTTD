/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file actions_orders.cpp Typed orders, shared-order and timetable integration. */

#include "../stdafx.h"
#include "actions_internal.h"
#include "actions_vehicle_scope.h"
#include "actions_limits.h"
#include "../3rdparty/monocypher/monocypher.h"
#include "../cargo_type.h"
#include "../cargotype.h"
#include "../command_func.h"
#include "../depot_base.h"
#include "../depot_map.h"
#include "../order_cmd.h"
#include "../station_base.h"
#include "../station_map.h"
#include "../string_func.h"
#include "../timetable_cmd.h"
#include "../vehicle_base.h"
#include "../vehicle_cmd.h"
#include "../vehicle_func.h"
#include "../waypoint_base.h"

#include "../safeguards.h"

namespace Integration::Detail {
namespace {



const Vehicle *PrimaryVehicle(const Parameters &parameters, const char *key = "vehicle_id")
{
	const Vehicle *vehicle = Vehicle::Get(parameters.Vehicle(key));
	if (!vehicle->IsPrimaryVehicle()) throw InvalidParameters("Orders require a primary vehicle");
	return vehicle;
}

/** Ordinary OrdersWindow permits reading another company's primary vehicle. */
const Vehicle *ReadablePrimaryVehicle(const Json &parameters)
{
	const Json &handle = parameters.at("vehicle_id");
	if (!handle.is_object() || handle.size() != 3 || !handle.contains("id") ||
			!handle.contains("generation") || !handle.contains("engine_instance_id") ||
			!handle.at("id").is_number_integer() || !handle.at("generation").is_number_integer() ||
			!handle.at("engine_instance_id").is_string()) throw InvalidParameters("Invalid vehicle handle");
	Parameters fields(handle);
	const uint32_t raw_id = static_cast<uint32_t>(fields.Int("id", 0, VehicleID::Invalid().base() - 1));
	const std::string instance = fields.Text("engine_instance_id", "", 256);
	if (instance.empty() || instance != InstanceId()) throw InvalidParameters("Vehicle target belongs to another engine instance");
	if (!handle.at("generation").is_number_unsigned() && handle.at("generation").get<int64_t>() <= 0) throw InvalidParameters("Invalid vehicle generation");
	const uint64_t generation = handle.at("generation").get<uint64_t>();
	const Vehicle *vehicle = Vehicle::GetIfValid(VehicleID(raw_id));
	if (vehicle == nullptr || vehicle->GetInstanceGeneration() != generation ||
			!IsCompanyBuildableVehicleType(vehicle) || !vehicle->IsPrimaryVehicle()) throw InvalidParameters("Vehicle target is stale or unavailable");
	return vehicle;
}


/** Stream bounded native values; generation-free proofs are comparable across replicas. */
std::string OrderVersion(const Vehicle *vehicle, const std::vector<const Vehicle *> &shared, bool authoritative)
{
	crypto_blake2b_ctx hash;
	crypto_blake2b_init(&hash, 32);
	auto bytes = [&](std::span<const uint8_t> value) { crypto_blake2b_update(&hash, value.data(), value.size()); };
	auto value = [&](const auto &item) { bytes(EndianBufferWriter<CommandDataBuffer>::FromValue(item)); };
	static constexpr std::string_view DOMAIN = "OpenTTD integration orders v1";
	bytes({reinterpret_cast<const uint8_t *>(DOMAIN.data()), DOMAIN.size()});
	value(authoritative);
	if (authoritative) {
		std::string instance(InstanceId());
		value(instance);
		value(vehicle->orders != nullptr);
		if (vehicle->orders != nullptr) {
			value(vehicle->orders->index.base());
			value(vehicle->orders->GetInstanceGeneration());
		}
	}
	value(vehicle->GetNumOrders());
	for (const Order &order : vehicle->Orders()) value(order);
	value(static_cast<uint32_t>(shared.size()));
	for (const Vehicle *member : shared) {
		value(member->index.base());
		if (authoritative) value(member->GetInstanceGeneration());
	}
	std::array<uint8_t, 32> result;
	crypto_blake2b_final(&hash, result.data());
	return std::string(authoritative ? "ov1:" : "oi1:") + FormatArrayAsHex(result);
}

void RequireVersion(const Json &parameters, const Vehicle *vehicle, const char *key = "order_version")
{
	Parameters p(parameters);
	if (p.Text(key, "", 68) != OrderVersion(vehicle, NativeSharedVehicles(vehicle), true)) throw InvalidParameters(std::string("Stale or missing ") + key + "; query orders again");
}

VehicleOrderID Position(const Parameters &p, const Vehicle *vehicle, const char *key = "position", bool insertion = false)
{
	int maximum = static_cast<int>(vehicle->GetNumOrders()) - (insertion ? 0 : 1);
	if (maximum < 0) throw InvalidParameters("Vehicle has no orders");
	return static_cast<VehicleOrderID>(p.Int(key, 0, std::min<int>(MAX_VEH_ORDER_ID, maximum), insertion ? std::optional<int64_t>(maximum) : std::nullopt));
}

template <typename T, size_t N>
T Choice(const std::string &name, const std::array<std::pair<std::string_view, T>, N> &choices, const char *field)
{
	for (const auto &[label, value] : choices) if (name == label) return value;
	throw InvalidParameters(std::string("Invalid ") + field);
}

constexpr std::array<std::pair<std::string_view, OrderLoadType>, 4> LOAD = {{{"if_possible", OrderLoadType::LoadIfPossible}, {"full", OrderLoadType::FullLoad}, {"full_any", OrderLoadType::FullLoadAny}, {"none", OrderLoadType::NoLoad}}};
constexpr std::array<std::pair<std::string_view, OrderUnloadType>, 4> UNLOAD = {{{"if_possible", OrderUnloadType::UnloadIfPossible}, {"all", OrderUnloadType::Unload}, {"transfer", OrderUnloadType::Transfer}, {"none", OrderUnloadType::NoUnload}}};
constexpr std::array<std::pair<std::string_view, OrderStopLocation>, 3> STOP = {{{"near", OrderStopLocation::NearEnd}, {"middle", OrderStopLocation::Middle}, {"far", OrderStopLocation::FarEnd}}};
constexpr std::array<std::pair<std::string_view, OrderDepotAction>, 4> DEPOT = {{{"always", OrderDepotAction::AlwaysGo}, {"service", OrderDepotAction::Service}, {"halt", OrderDepotAction::Stop}, {"unbunch", OrderDepotAction::Unbunch}}};
constexpr std::array<std::pair<std::string_view, OrderConditionVariable>, 9> VARIABLE = {{{"load_percentage", OrderConditionVariable::LoadPercentage}, {"reliability", OrderConditionVariable::Reliability}, {"max_speed", OrderConditionVariable::MaxSpeed}, {"age", OrderConditionVariable::Age}, {"requires_service", OrderConditionVariable::RequiresService}, {"unconditional", OrderConditionVariable::Unconditionally}, {"remaining_lifetime", OrderConditionVariable::RemainingLifetime}, {"max_reliability", OrderConditionVariable::MaxReliability}, {"driving_backwards", OrderConditionVariable::DrivingBackwards}}};
constexpr std::array<std::pair<std::string_view, OrderConditionComparator>, 8> COMPARATOR = {{{"eq", OrderConditionComparator::Equal}, {"ne", OrderConditionComparator::NotEqual}, {"lt", OrderConditionComparator::LessThan}, {"le", OrderConditionComparator::LessThanOrEqual}, {"gt", OrderConditionComparator::MoreThan}, {"ge", OrderConditionComparator::MoreThanOrEqual}, {"is_true", OrderConditionComparator::IsTrue}, {"is_false", OrderConditionComparator::IsFalse}}};

template <typename T, size_t N>
std::string_view Label(T value, const std::array<std::pair<std::string_view, T>, N> &choices)
{
	for (const auto &[label, candidate] : choices) if (value == candidate) return label;
	return "unknown";
}

OrderNonStopFlags Routing(const std::string &name)
{
	if (name == "stop") return {};
	if (name == "non_stop") return OrderNonStopFlag::NonStop;
	if (name == "via") return OrderNonStopFlag::GoVia;
	if (name == "non_stop_via") return {OrderNonStopFlag::NonStop, OrderNonStopFlag::GoVia};
	throw InvalidParameters("Invalid routing");
}

std::string_view RoutingLabel(OrderNonStopFlags flags)
{
	if (flags.Test(OrderNonStopFlag::GoVia)) return flags.Test(OrderNonStopFlag::NonStop) ? "non_stop_via" : "via";
	return flags.Test(OrderNonStopFlag::NonStop) ? "non_stop" : "stop";
}

Order MakeDestinationOrder(const Json &parameters, const Parameters &p, const Vehicle *vehicle)
{
	Order order;
	const std::string type = p.Text("destination_type", "station");
	if (parameters.contains("routing") && parameters.contains("non_stop")) throw InvalidParameters("Use either routing or legacy non_stop");
	if (parameters.contains("depot_action") && (parameters.contains("service") || parameters.contains("halt"))) throw InvalidParameters("Use either depot_action or legacy service/halt");
	if (type == "nearest_depot" && parameters.contains("destination")) throw InvalidParameters("nearest_depot has no fixed destination");
	if (type == "depot" || type == "nearest_depot") {
		OrderDepotTypeFlags flags = OrderDepotTypeFlag::PartOfOrders;
		OrderDepotActionFlags action;
		if (type == "nearest_depot") action.Set(OrderDepotActionFlag::NearestDepot);
		auto policy = Choice(p.Text("depot_action", "always"), DEPOT, "depot_action");
		if (!parameters.contains("depot_action")) {
			if (p.Bool("service") && p.Bool("halt")) throw InvalidParameters("service and halt are mutually exclusive");
			if (p.Bool("service")) policy = OrderDepotAction::Service;
			if (p.Bool("halt")) policy = OrderDepotAction::Stop;
		}
		if (policy == OrderDepotAction::Service) flags.Set(OrderDepotTypeFlag::Service);
		if (policy == OrderDepotAction::Stop) action.Set(OrderDepotActionFlag::Halt);
		if (policy == OrderDepotAction::Unbunch) action.Set(OrderDepotActionFlag::Unbunch);
		DestinationID destination{};
		if (type == "depot") {
			auto tile = p.Tile("destination");
			if (vehicle->type == VehicleType::Aircraft) {
				if (!IsHangarTile(tile)) throw InvalidParameters("Aircraft depot destination must be a hangar");
				destination = GetStationIndex(tile);
			} else {
				if (!IsDepotTile(tile) || IsHangarTile(tile)) throw InvalidParameters("Destination is not a ground or water depot");
				destination = GetDepotIndex(tile);
			}
		}
		order.MakeGoToDepot(destination, flags, {}, action);
	} else if (type == "station" || type == "waypoint") {
		auto tile = p.Tile("destination");
		if (!IsTileType(tile, TileType::Station)) throw InvalidParameters("Destination is not a station tile");
		if (type == "station") order.MakeGoToStation(GetStationIndex(tile)); else order.MakeGoToWaypoint(GetStationIndex(tile));
	} else {
		throw InvalidParameters("destination_type must be station, waypoint, depot or nearest_depot");
	}
	if (type != "depot" && type != "nearest_depot" && (parameters.contains("service") || parameters.contains("halt") || parameters.contains("depot_action"))) throw InvalidParameters("Depot policy requires a depot order");
	if (type != "station" && (parameters.contains("load") || parameters.contains("unload") || parameters.contains("stop_location"))) throw InvalidParameters("Loading and stop-location options require a station order");
	if (type == "station") {
		order.SetLoadType(Choice(p.Text("load", "if_possible"), LOAD, "load"));
		order.SetUnloadType(Choice(p.Text("unload", "if_possible"), UNLOAD, "unload"));
		order.SetStopLocation(Choice(p.Text("stop_location", "far"), STOP, "stop_location"));
	}
	order.SetNonStopType(parameters.contains("routing") ? Routing(p.Text("routing")) : p.Bool("non_stop") ? OrderNonStopFlags(OrderNonStopFlag::NonStop) : OrderNonStopFlags{});
	return order;
}

Order MakeConditionalOrder(const Parameters &p, const Vehicle *vehicle)
{
	auto variable = Choice(p.Text("variable"), VARIABLE, "variable");
	bool boolean = variable == OrderConditionVariable::RequiresService || variable == OrderConditionVariable::DrivingBackwards;
	auto comparator = Choice(p.Text("comparator", boolean ? "is_true" : "eq"), COMPARATOR, "comparator");
	uint16_t value = p.Int("value", 0, 2047, 0);
	bool boolean_comparator = comparator == OrderConditionComparator::IsTrue || comparator == OrderConditionComparator::IsFalse;
	if (boolean != boolean_comparator) throw InvalidParameters("Comparator is incompatible with the condition variable");
	if (boolean && value != 0) throw InvalidParameters("Boolean conditions have no numeric value");
	if (variable == OrderConditionVariable::Unconditionally && (comparator != OrderConditionComparator::Equal || value != 0)) throw InvalidParameters("Unconditional orders require eq and zero");
	if ((variable == OrderConditionVariable::LoadPercentage || variable == OrderConditionVariable::Reliability || variable == OrderConditionVariable::MaxReliability) && value > 100) throw InvalidParameters("Percentage condition values must be at most 100");
	if (variable == OrderConditionVariable::DrivingBackwards && vehicle->type != VehicleType::Train) throw InvalidParameters("driving_backwards requires a train");
	auto target = static_cast<VehicleOrderID>(p.Int("target_position", 0, std::max<int>(0, vehicle->GetNumOrders() - 1)));
	Order order;
	order.MakeConditional(target);
	order.SetConditionVariable(variable);
	order.SetConditionComparator(comparator);
	order.SetConditionValue(value);
	return order;
}

std::pair<ModifyOrderFlags, uint16_t> Modification(const Json &parameters, const Parameters &p, const Vehicle *vehicle)
{
	auto field = p.Text("field");
	const auto &value = parameters.at("value");
	auto text = [&]() { if (!value.is_string()) throw InvalidParameters("Expected string value"); return value.get<std::string>(); };
	if (field == "load") return {MOF_LOAD, to_underlying(Choice(text(), LOAD, "load value"))};
	if (field == "unload") return {MOF_UNLOAD, to_underlying(Choice(text(), UNLOAD, "unload value"))};
	if (field == "routing") return {MOF_NON_STOP, Routing(text()).base()};
	if (field == "stop_location") return {MOF_STOP_LOCATION, to_underlying(Choice(text(), STOP, "stop_location value"))};
	if (field == "depot_action") return {MOF_DEPOT_ACTION, to_underlying(Choice(text(), DEPOT, "depot_action value"))};
	if (field == "condition_variable") return {MOF_COND_VARIABLE, to_underlying(Choice(text(), VARIABLE, "condition_variable value"))};
	if (field == "condition_comparator") return {MOF_COND_COMPARATOR, to_underlying(Choice(text(), COMPARATOR, "condition_comparator value"))};
	if (field == "condition_value") return {MOF_COND_VALUE, static_cast<uint16_t>(p.Int("value", 0, 2047))};
	if (field == "condition_target_position") return {MOF_COND_DESTINATION, Position(p, vehicle, "value")};
	throw InvalidParameters("Invalid order modification field");
}

Json TimetableControl(const std::vector<const Vehicle *> &shared)
{
	crypto_blake2b_ctx hash;
	crypto_blake2b_init(&hash, 32);
	for (const Vehicle *vehicle : shared) {
		auto data = EndianBufferWriter<CommandDataBuffer>::FromValue(std::tuple{vehicle->index.base(), vehicle->vehicle_flags.Test(VehicleFlag::AutofillTimetable), vehicle->vehicle_flags.Test(VehicleFlag::AutofillPreserveWaitTime)});
		crypto_blake2b_update(&hash, data.data(), data.size());
	}
	std::array<uint8_t, 32> digest;
	crypto_blake2b_final(&hash, digest.data());
	return FormatArrayAsHex(digest);
}

Json OrderLease(const Vehicle *vehicle, bool authoritative, bool autofill)
{
	auto shared = NativeSharedVehicles(vehicle);
	Json result = {{"vehicle_id", vehicle->index.base()}, {authoritative ? "order_version" : "content_digest", OrderVersion(vehicle, shared, authoritative)}, {"shared_count", shared.size()}};
	if (autofill) result["autofill_digest"] = TimetableControl(shared);
	return result;
}

/** Resolve only a current, fixed native destination; dynamic depot choices stay absent. */
Json FixedDestinationEntity(const Order &order, const Vehicle *vehicle)
{
	if (order.IsType(OT_GOTO_STATION) || order.IsType(OT_IMPLICIT)) {
		const Station *station = Station::GetIfValid(order.GetDestination().ToStationID());
		return station == nullptr ? Json(nullptr) : Json{{"kind", "station"}, {"handle", Handle(station)}};
	}
	if (order.IsType(OT_GOTO_WAYPOINT)) {
		const Waypoint *waypoint = Waypoint::GetIfValid(order.GetDestination().ToStationID());
		return waypoint == nullptr ? Json(nullptr) : Json{{"kind", "waypoint"}, {"handle", Handle(waypoint)}};
	}
	if (!order.IsType(OT_GOTO_DEPOT) ||
			!order.GetDepotOrderType().Test(OrderDepotTypeFlag::PartOfOrders) ||
			order.GetDepotActionType().Test(OrderDepotActionFlag::NearestDepot)) return nullptr;
	if (vehicle->type == VehicleType::Aircraft) {
		/* Aircraft depot destinations are airport station IDs, not DepotIDs. */
		const Station *station = Station::GetIfValid(order.GetDestination().ToStationID());
		return station == nullptr || !station->facilities.Test(StationFacility::Airport) || !station->airport.HasHangar() ?
			Json(nullptr) : Json{{"kind", "station"}, {"handle", Handle(station)}};
	}
	const Depot *depot = Depot::GetIfValid(order.GetDestination().ToDepotID());
	const TransportType expected_type = vehicle->type == VehicleType::Train ? TransportType::Rail :
		vehicle->type == VehicleType::Road ? TransportType::Road : TransportType::Water;
	if (depot == nullptr || depot->xy == INVALID_TILE || !IsValidTile(depot->xy) ||
			!IsDepotTypeTile(depot->xy, expected_type) || GetDepotIndex(depot->xy) != depot->index) return nullptr;
	return {{"kind", "depot"}, {"handle", Handle(depot)}};
}

const char *ExecutionKind(OrderType type)
{
	switch (type) {
		case OT_NOTHING: return "none";
		case OT_GOTO_STATION: return "station";
		case OT_GOTO_DEPOT: return "depot";
		case OT_LOADING: return "loading";
		case OT_LEAVESTATION: return "leaving_station";
		case OT_DUMMY: return "dummy";
		case OT_GOTO_WAYPOINT: return "waypoint";
		case OT_CONDITIONAL: return "conditional";
		case OT_IMPLICIT: return "implicit";
		default: return "other";
	}
}

Json CurrentExecution(const Vehicle *vehicle)
{
	const Order &order = vehicle->current_order;
	Json result = {{"type", to_underlying(order.GetType())}, {"kind", ExecutionKind(order.GetType())},
		{"real_position", vehicle->cur_real_order_index < vehicle->GetNumOrders() ? Json(vehicle->cur_real_order_index) : Json(nullptr)}};
	Json destination = FixedDestinationEntity(order, vehicle);
	if (!destination.is_null()) result["destination_entity"] = std::move(destination);
	return result;
}

Json OrderSnapshot(const Order &order, const Vehicle *vehicle, uint position, bool live_detail = false)
{
	Json item = {{"position", position}, {"type", to_underlying(order.GetType())}, {"implicit", order.IsType(OT_IMPLICIT)}, {"destination_id", order.GetDestination().base()}, {"wait_ticks", order.GetWaitTime()}, {"travel_ticks", order.GetTravelTime()}, {"wait_timetabled", order.IsWaitTimetabled()}, {"travel_timetabled", order.IsTravelTimetabled()}, {"speed_limit", order.GetMaxSpeed() == UINT16_MAX ? Json(nullptr) : Json(order.GetMaxSpeed())}};
	if (order.IsType(OT_CONDITIONAL)) {
		item["kind"] = "conditional";
		item["condition"] = {{"variable", Label(order.GetConditionVariable(), VARIABLE)}, {"comparator", Label(order.GetConditionComparator(), COMPARATOR)}, {"value", order.GetConditionValue()}, {"target_position", order.GetConditionSkipToOrder()}};
	} else if (order.IsType(OT_GOTO_STATION) || order.IsType(OT_GOTO_DEPOT) || order.IsType(OT_GOTO_WAYPOINT) || order.IsType(OT_IMPLICIT)) {
		item["kind"] = order.IsType(OT_GOTO_DEPOT) ? "depot" : order.IsType(OT_GOTO_WAYPOINT) ? "waypoint" : order.IsType(OT_IMPLICIT) ? "implicit" : "station";
		item["routing"] = RoutingLabel(order.GetNonStopType());
		if (order.IsType(OT_GOTO_STATION) || order.IsType(OT_IMPLICIT)) {
			/* Preserve the existing numeric fields while adding semantic choices. */
			item["load"] = to_underlying(order.GetLoadType());
			item["unload"] = to_underlying(order.GetUnloadType());
			item["load_policy"] = Label(order.GetLoadType(), LOAD);
			item["unload_policy"] = Label(order.GetUnloadType(), UNLOAD);
			item["stop_location"] = Label(order.GetStopLocation(), STOP);
		}
		if (order.IsType(OT_GOTO_DEPOT)) {
			item["nearest_depot"] = order.GetDepotActionType().Test(OrderDepotActionFlag::NearestDepot);
			item["depot_action"] = order.GetDepotActionType().Test(OrderDepotActionFlag::Halt) ? "halt" : order.GetDepotActionType().Test(OrderDepotActionFlag::Unbunch) ? "unbunch" : order.GetDepotOrderType().Test(OrderDepotTypeFlag::Service) ? "service" : "always";
		}
		if (!order.IsType(OT_GOTO_DEPOT) || !order.GetDepotActionType().Test(OrderDepotActionFlag::NearestDepot)) {
			auto destination = order.GetLocation(vehicle, false);
			if (destination != INVALID_TILE && IsValidTile(destination)) item["destination"] = TileCoordinates(destination);
		}
	} else {
		item["kind"] = "other";
	}
	item["non_stop"] = order.GetNonStopType().base();
	item["refit_cargo_id"] = to_underlying(order.GetRefitCargo());
	item["refit"] = order.GetRefitCargo() == CARGO_NO_REFIT ? Json{{"mode", "none"}} : order.GetRefitCargo() == CARGO_AUTO_REFIT ? Json{{"mode", "auto"}} : Json{{"mode", "cargo"}, {"cargo_id", to_underlying(order.GetRefitCargo())}};
	if (live_detail) {
		Json destination = FixedDestinationEntity(order, vehicle);
		if (!destination.is_null()) item["destination_entity"] = std::move(destination);
	}
	return item;
}

template <Commands Tcommand>
typename CommandTraits<Tcommand>::Args Decode(const CommandDataBuffer &data)
{
	using Args = typename CommandTraits<Tcommand>::Args;
	if (data.size() > 8192) throw InvalidParameters("Native order command data is oversized");
	auto args = EndianBufferReader::ToValue<Args>(data);
	if (EndianBufferWriter<CommandDataBuffer>::FromValue(args) != data) throw InvalidParameters("Native order command data is malformed or noncanonical");
	return args;
}

} // namespace

std::string VehicleOrderControlVersion(const Vehicle *vehicle)
{
	if (vehicle == nullptr || vehicle->owner != _current_company || !IsCompanyBuildableVehicleType(vehicle) || !vehicle->IsPrimaryVehicle()) throw InvalidParameters("Orders require an owned primary vehicle");
	return OrderVersion(vehicle, NativeSharedVehicles(vehicle), true);
}

std::optional<ActionResult> DispatchOrdersAction(const std::string &action, const Json &parameters, ActionInvocation &invocation)
{
	if (!action.starts_with("order.") && !action.starts_with("timetable.")) return std::nullopt;
	Parameters p(parameters);
	const Vehicle *vehicle = PrimaryVehicle(p);
	RequireVersion(parameters, vehicle);
	auto id = vehicle->index;
	if (action == "order.copy") {
		const Vehicle *source = PrimaryVehicle(p, "source_vehicle_id");
		RequireVersion(parameters, source, "source_order_version");
		return invocation.Post<Commands::CloneOrder>(TileIndex{0}, p.Bool("share") ? CO_SHARE : CO_COPY, id, source->index);
	}
	if (action == "order.unshare") {
		auto mode = p.Text("mode", "clear");
		if (mode == "clear") return invocation.Post<Commands::CloneOrder>(TileIndex{0}, CO_UNSHARE, id, id);
		if (mode != "keep") throw InvalidParameters("mode must be keep or clear");
		auto shared = NativeSharedVehicles(vehicle);
		auto source = std::ranges::find_if(shared, [&](const Vehicle *member) { return member != vehicle; });
		if (source == shared.end()) return ActionResult{false, "Vehicle does not share its orders", "not_shared"};
		return invocation.Post<Commands::CloneOrder>(TileIndex{0}, CO_COPY, id, (*source)->index);
	}
	if (action == "order.insert" || action == "order.conditional.insert") {
		Order order = action == "order.insert" ? MakeDestinationOrder(parameters, p, vehicle) : MakeConditionalOrder(p, vehicle);
		return invocation.Post<Commands::InsertOrder>(TileIndex{0}, id, Position(p, vehicle, "position", true), order);
	}
	if (action == "order.delete") return invocation.Post<Commands::DeleteOrder>(TileIndex{0}, id, Position(p, vehicle));
	if (action == "order.skip") return invocation.Post<Commands::SkipToOrder>(TileIndex{0}, id, Position(p, vehicle));
	if (action == "order.move") return invocation.Post<Commands::MoveOrder>(TileIndex{0}, id, Position(p, vehicle), Position(p, vehicle, "target_position"));
	if (action == "order.modify") {
		auto [field, value] = Modification(parameters, p, vehicle);
		return invocation.Post<Commands::ModifyOrder>(TileIndex{0}, id, Position(p, vehicle), field, value);
	}
	if (action == "order.refit") {
		auto mode = p.Text("mode");
		CargoType cargo;
		if (mode == "none") cargo = CARGO_NO_REFIT;
		else if (mode == "auto") cargo = CARGO_AUTO_REFIT;
		else if (mode == "cargo") {
			cargo = static_cast<CargoType>(p.Int("cargo_id", 0, NUM_CARGO - 1));
			if (!CargoSpec::Get(cargo)->IsValid()) throw InvalidParameters("Invalid cargo_id");
		} else throw InvalidParameters("mode must be cargo, auto or none");
		if (mode != "cargo" && parameters.contains("cargo_id")) throw InvalidParameters("cargo_id requires cargo refit mode");
		return invocation.Post<Commands::OrderRefit>(TileIndex{0}, id, Position(p, vehicle), cargo);
	}
	if (action == "timetable.set") {
		auto field = p.Text("field");
		ModifyTimetableFlags native;
		if (field == "wait_ticks") native = MTF_WAIT_TIME;
		else if (field == "travel_ticks") native = MTF_TRAVEL_TIME;
		else if (field == "speed_limit") native = MTF_TRAVEL_SPEED;
		else throw InvalidParameters("field must be wait_ticks, travel_ticks or speed_limit");
		return invocation.Post<Commands::ChangeTimetable>(id, Position(p, vehicle), native, p.Int("value", 0, UINT16_MAX));
	}
	if (action == "timetable.start") {
		const auto &value = parameters.at("start_tick");
		if (!value.is_number_integer() || (!value.is_number_unsigned() && value.get<int64_t>() < 0)) throw InvalidParameters("start_tick must be a nonnegative integer");
		return invocation.Post<Commands::SetTimetableStart>(id, p.Bool("shared"), value.get<uint64_t>());
	}
	if (action == "timetable.reset_lateness") return invocation.Post<Commands::SetVehicleOnTime>(id, p.Bool("shared"));
	if (action == "timetable.autofill") {
		bool enabled = p.Bool("enabled"), preserve = p.Bool("preserve_wait_time");
		if (!enabled && preserve) throw InvalidParameters("preserve_wait_time requires enabled autofill");
		if (enabled && vehicle->vehicle_flags.Test(VehicleFlag::AutofillPreserveWaitTime) && !preserve) throw InvalidParameters("Stop autofill before changing preserve_wait_time from true to false");
		return invocation.Post<Commands::AutofillTimetable>(id, enabled, preserve);
	}
	return std::nullopt;
}

std::optional<Json> QueryOrdersState(const std::string &query, const Json &parameters)
{
	if (query != "orders" && query != "timetable") return std::nullopt;
	Parameters p(parameters);
	const Vehicle *vehicle = query == "orders" ? ReadablePrimaryVehicle(parameters) : PrimaryVehicle(p);
	const bool live_detail = p.Bool("include_live_detail");
	auto shared = NativeSharedVehicles(vehicle);
	Json result = SnapshotBase();
	result["vehicle_id"] = Handle(vehicle);
	result["order_version"] = OrderVersion(vehicle, shared, true);
	result["order_count"] = vehicle->GetNumOrders();
	result["manual_order_count"] = vehicle->GetNumManualOrders();
	result["current_order"] = vehicle->cur_real_order_index;
	if (live_detail) result["current_execution"] = CurrentExecution(vehicle);
	int after = p.Int("after_position", -1, MAX_VEH_ORDER_ID - 1, -1);
	size_t limit = p.Int("limit", 1, live_detail ? 16 : 64, live_detail ? 16 : 64);
	Json orders = Json::array();
	uint position = 0;
	for (const Order &order : vehicle->Orders()) {
		if (static_cast<int>(position) > after && orders.size() < limit) orders.push_back(OrderSnapshot(order, vehicle, position, live_detail));
		++position;
	}
	result["next_after_position"] = !orders.empty() && orders.back()["position"].get<uint>() + 1 < vehicle->GetNumOrders() ? orders.back()["position"] : Json(nullptr);
	result["orders"] = std::move(orders);
	result["shared_count"] = shared.size();
	int64_t shared_after = p.Int("shared_after_id", -1, UINT32_MAX, -1);
	size_t shared_limit = p.Int("shared_limit", 1, live_detail ? 16 : 64, live_detail ? 16 : 64);
	Json members = Json::array();
	bool more = false;
	for (const Vehicle *member : shared) {
		if (static_cast<int64_t>(member->index.base()) <= shared_after) continue;
		if (members.size() == shared_limit) { more = true; break; }
		members.push_back(Handle(member));
	}
	result["next_shared_after_id"] = more ? members.back()["id"] : Json(nullptr);
	result["shared_vehicles"] = std::move(members);
	if (query == "timetable") {
		result["started"] = vehicle->vehicle_flags.Test(VehicleFlag::TimetableStarted);
		result["start_tick"] = vehicle->timetable_start;
		result["lateness_ticks"] = vehicle->lateness_counter;
		result["autofill"] = vehicle->vehicle_flags.Test(VehicleFlag::AutofillTimetable);
		result["preserve_wait_time"] = vehicle->vehicle_flags.Test(VehicleFlag::AutofillPreserveWaitTime);
		result["complete"] = vehicle->orders != nullptr && vehicle->orders->IsCompleteTimetable();
		result["duration_ticks"] = vehicle->orders == nullptr ? 0 : vehicle->orders->GetTimetableDurationIncomplete();
		result["speed_unit"] = "kmh_ish";
	}
	if (live_detail && result.dump().size() > 40 * 1024) throw InvalidParameters("Detailed orders exceed the observation byte budget");
	return result;
}

std::optional<Json> CaptureOrdersTargets(const std::string &action, const Json &parameters)
{
	bool family = action.starts_with("order.") || action.starts_with("timetable.");
	if (!family && action != "vehicle.clone") return std::nullopt;
	Parameters p(parameters);
	const Vehicle *vehicle = PrimaryVehicle(p);
	if (family) RequireVersion(parameters, vehicle);
	Json state = {{"orders", Json::array({OrderLease(vehicle, true, action == "timetable.autofill")})}};
	if (action == "order.copy") {
		const Vehicle *source = PrimaryVehicle(p, "source_vehicle_id");
		RequireVersion(parameters, source, "source_order_version");
		state["orders"].push_back(OrderLease(source, true, false));
	}
	return state;
}

std::optional<Json> OrdersIntentState(Commands command, const CommandDataBuffer &data)
{
	std::vector<VehicleID> vehicles;
	switch (command) {
		case Commands::InsertOrder: vehicles.push_back(std::get<0>(Decode<Commands::InsertOrder>(data))); break;
		case Commands::DeleteOrder: vehicles.push_back(std::get<0>(Decode<Commands::DeleteOrder>(data))); break;
		case Commands::SkipToOrder: vehicles.push_back(std::get<0>(Decode<Commands::SkipToOrder>(data))); break;
		case Commands::MoveOrder: vehicles.push_back(std::get<0>(Decode<Commands::MoveOrder>(data))); break;
		case Commands::ModifyOrder: vehicles.push_back(std::get<0>(Decode<Commands::ModifyOrder>(data))); break;
		case Commands::OrderRefit: vehicles.push_back(std::get<0>(Decode<Commands::OrderRefit>(data))); break;
		case Commands::ChangeTimetable: vehicles.push_back(std::get<0>(Decode<Commands::ChangeTimetable>(data))); break;
		case Commands::SetVehicleOnTime: vehicles.push_back(std::get<0>(Decode<Commands::SetVehicleOnTime>(data))); break;
		case Commands::SetTimetableStart: vehicles.push_back(std::get<0>(Decode<Commands::SetTimetableStart>(data))); break;
		case Commands::AutofillTimetable: vehicles.push_back(std::get<0>(Decode<Commands::AutofillTimetable>(data))); break;
		case Commands::CloneOrder: {
			auto args = Decode<Commands::CloneOrder>(data);
			vehicles.push_back(std::get<1>(args));
			/* Native CO_UNSHARE deliberately ignores veh_src. */
			if (std::get<0>(args) != CO_UNSHARE) vehicles.push_back(std::get<2>(args));
			break;
		}
		case Commands::CloneVehicle: vehicles.push_back(std::get<1>(Decode<Commands::CloneVehicle>(data))); break;
		default: return std::nullopt;
	}
	std::ranges::sort(vehicles);
	vehicles.erase(std::unique(vehicles.begin(), vehicles.end()), vehicles.end());
	Json state = {{"orders", Json::array()}};
	for (auto id : vehicles) {
		const Vehicle *vehicle = Vehicle::GetIfValid(id);
		if (vehicle == nullptr || !vehicle->IsPrimaryVehicle() || vehicle->owner != _current_company) throw InvalidParameters("Native orders require an owned primary vehicle");
		state["orders"].push_back(OrderLease(vehicle, false, command == Commands::AutofillTimetable));
	}
	return state;
}

} // namespace Integration::Detail
