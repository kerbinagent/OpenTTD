/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file actions_internal.h Shared readers and explicit gameplay family entry points. */

#ifndef INTEGRATION_ACTIONS_INTERNAL_H
#define INTEGRATION_ACTIONS_INTERNAL_H

#include "actions.h"
#include "integration_bridge.h"
#include "../command_func.h"
#include "../direction_type.h"
#include "../rail_type.h"
#include "../road_type.h"
#include "../station_type.h"
#include "../strings_type.h"
#include "../tile_type.h"
#include "../track_type.h"
#include "../transport_type.h"
#include "../vehicle_type.h"

#include <optional>
#include <span>
#include <stdexcept>
#include <string_view>
#include <utility>

struct SettingDesc;

namespace Integration::Detail {

class InvalidParameters : public std::runtime_error {
public:
	using std::runtime_error::runtime_error;
};

/** Readers run only after central capability-schema validation. */
class Parameters {
	const Json &data;
public:
	explicit Parameters(const Json &data) : data(data) {}
	int64_t Int(const char *key, int64_t minimum, int64_t maximum, std::optional<int64_t> fallback = std::nullopt) const;
	bool Bool(const char *key, bool fallback = false) const;
	std::string Text(const char *key, const std::string &fallback = "", size_t maximum = 128) const;
	TileIndex Tile(const char *key) const;
	StationID Station(const char *key = "station_id", bool optional = false) const;
	VehicleID Vehicle(const char *key = "vehicle_id") const;
	RoadType Road() const;
	RailType Rail() const;
	Axis GetAxis() const;
	DiagDirection Direction() const;
	Track GetTrack() const;
	RoadStopType StopType() const;
	TransportType Transport(bool water) const;
};

void BoundedArea(TileIndex start, TileIndex end, uint max_area = 4096);
void RequireCompany();
ActionResult Submitted(bool accepted);

/** Exact native argument conversions and Shift test path, without UI or a Post. */
template <Commands C, typename Proc = typename CommandTraits<C>::ProcType,
	bool HasTile = !GetCommandFlags<C>().Test(CommandFlag::Location)>
struct NativeEstimateCommand;

template <Commands C, typename Ret, typename... Args>
struct NativeEstimateCommand<C, Ret(*)(DoCommandFlags, Args...), true> : private CommandHelper<C, Ret(*)(DoCommandFlags, Args...), true> {
	static CommandCost Run(Args... args)
	{
		std::tuple<Args...> native_args(std::forward<Args>(args)...);
		TileIndex location{};
		if constexpr (std::is_same_v<TileIndex, std::tuple_element_t<0, std::tuple<Args...>>>) location = std::get<0>(native_args);
		return Estimate(location, std::move(native_args));
	}

protected:
	static CommandCost Estimate(TileIndex location, std::tuple<Args...> args)
	{
		/* Match InternalPost before Unsafe: it deliberately omits these checks. */
		if (location != 0 && (location >= Map::Size() || (!IsValidTile(location) && !GetCommandFlags<C>().Test(CommandFlag::AllTiles)))) return CMD_ERROR;
		if constexpr (GetCommandFlags<C>().Test(CommandFlag::ClientID)) NativeEstimateCommand::SetClientIds(args, std::index_sequence_for<Args...>{});
		auto result = NativeEstimateCommand::template Unsafe<CommandCallback>(StringID{}, nullptr, false, true, location, std::move(args));
		return ExtractCommandCost(result);
	}
};

template <Commands C, typename Ret, typename... Args>
struct NativeEstimateCommand<C, Ret(*)(DoCommandFlags, Args...), false> : private NativeEstimateCommand<C, Ret(*)(DoCommandFlags, Args...), true> {
	static CommandCost Run(TileIndex location, Args... args)
	{
		return NativeEstimateCommand<C, Ret(*)(DoCommandFlags, Args...), true>::Estimate(location, std::tuple<Args...>(std::forward<Args>(args)...));
	}
};

/** One request-local choice at the existing single native-command leaf. */
class ActionInvocation {
public:
	enum class Mode { Submit, Estimate };
	struct Estimate {
		CommandCost cost;
		Commands command;
		bool no_test;
	};

	explicit ActionInvocation(Mode mode) : mode(mode) {}
	const std::optional<Estimate> &GetEstimate() const { return this->estimate; }
	unsigned Calls() const { return this->calls; }

	template <Commands C, typename... Args>
	ActionResult Post(Args &&... args)
	{
		if (++this->calls != 1) return {false, {}, "multiple_action_commands"};
		if (this->mode == Mode::Submit) return Submitted(Command<C>::Post(std::forward<Args>(args)...));
		if constexpr (GetCommandFlags<C>().Test(CommandFlag::NoEst)) {
			return {false, {}, "estimate_unavailable"};
		} else {
			this->estimate.emplace(Estimate{NativeEstimateCommand<C>::Run(std::forward<Args>(args)...), C, GetCommandFlags<C>().Test(CommandFlag::NoTest)});
			/* Accepted always means queued. EstimateAction consumes the record. */
			return {false, {}, "estimate_only"};
		}
	}

private:
	Mode mode;
	unsigned calls = 0;
	std::optional<Estimate> estimate;
};

Json TileCoordinates(TileIndex tile);
const char *VehicleTypeName(VehicleType type);
Json SnapshotBase();

template <typename T>
Json Handle(const T *entity)
{
	return {{"id", entity->index.base()}, {"generation", entity->GetInstanceGeneration()}, {"engine_instance_id", InstanceId()}};
}

/** Bound returned objects and examined slots independently for sparse pools. */
template <typename T, typename Filter, typename Serialize>
Json PoolPage(const Parameters &p, Filter filter, Serialize serialize)
{
	auto index = static_cast<uint64_t>(p.Int("after_id", -1, UINT32_MAX, -1) + 1);
	auto limit = static_cast<size_t>(p.Int("limit", 1, 256, 64));
	Json items = Json::array();
	uint scanned = 0;
	while (index < T::GetPoolSize() && scanned < 4096 && items.size() < limit) {
		const T *entity = T::GetIfValid(index);
		if (entity != nullptr && filter(entity)) items.push_back(serialize(entity));
		++index;
		++scanned;
	}
	return {{"items", std::move(items)}, {"next_after_id", index < T::GetPoolSize() ? Json(index - 1) : Json(nullptr)}};
}

/* Nullopt means this family does not handle the operation. Errors throw
 * InvalidParameters to the central boundary. These functions do not register
 * capabilities, change company context, or bypass command completion. */
std::optional<ActionResult> DispatchAccessAction(const std::string &action, const Json &parameters, ActionInvocation &invocation);
std::optional<Json> CaptureAccessTargets(const std::string &action, const Json &parameters);
std::optional<Json> AccessIntentState(Commands command, const CommandDataBuffer &data);
/** ClientID, never NetworkClientInfo pool index; private roster projection. */
Json NativeParticipantHandle(uint32_t client_id);

std::optional<ActionResult> DispatchOrdersAction(const std::string &action, const Json &parameters, ActionInvocation &invocation);
std::optional<Json> QueryOrdersState(const std::string &query, const Json &parameters);
std::optional<Json> CaptureOrdersTargets(const std::string &action, const Json &parameters);
std::optional<Json> OrdersIntentState(Commands command, const CommandDataBuffer &data);

std::optional<Json> QueryAccessState(const std::string &query, const Json &parameters);

std::optional<ActionResult> DispatchManagementAction(const std::string &action, const Json &parameters, ActionInvocation &invocation);
std::optional<Json> QueryManagementState(const std::string &query, const Json &parameters);
std::optional<Json> CaptureManagementTargets(const std::string &action, const Json &parameters);
std::optional<Json> ManagementIntentState(Commands command, const CommandDataBuffer &data);
/** Exact nine-key registry permission for a native GameOptions descriptor. */
bool IsAllowedCompanySetting(const SettingDesc *descriptor);

/** Finite loaded specification selection; native QueryCost decides current availability. */
struct StationSpecSelection {
	uint16_t spec_class;
	uint16_t spec_index;
};
StationSpecSelection ResolveRailStationSpecSelection(const Parameters &parameters, bool waypoint);
StationSpecSelection ResolveRoadStopSpecSelection(const Parameters &parameters, StationType type);

std::optional<ActionResult> DispatchConstructionAction(const std::string &action, const Json &parameters, ActionInvocation &invocation);
std::optional<Json> QueryConstructionState(const std::string &query, const Json &parameters);
std::optional<Json> CaptureConstructionTargets(const std::string &action, const Json &parameters);
std::optional<Json> ConstructionIntentState(Commands command, const CommandDataBuffer &data);
std::optional<std::vector<TileIndex>> ConstructionIntentTiles(Commands command, const CommandDataBuffer &data);

/** Generation-free fixed-industry geometry proof and local incarnation references. */
struct IndustryPlatformDependencies {
	Json proof;
	std::vector<CompanyID> infrastructure_owners;
};
std::optional<IndustryPlatformDependencies> CaptureIndustryPlatformDependencies(const CommandDataBuffer &data);

std::optional<ActionResult> DispatchCompanyAction(const std::string &action, const Json &parameters, ActionInvocation &invocation);
std::optional<Json> QueryCompanyState(const std::string &query, const Json &parameters);
std::optional<Json> CaptureCompanyTargets(const std::string &action, const Json &parameters);
std::optional<Json> CompanyIntentState(Commands command, const CommandDataBuffer &data);
std::span<const std::string_view> CompanyLiveryNames();
std::span<const std::string_view> CompanyColourNames();

std::optional<Json> QueryReportsState(const std::string &query, const Json &parameters);
std::optional<Json> QueryWorldDetailsState(const std::string &query, const Json &parameters);
std::optional<Json> QueryStationDetailsState(const std::string &query, const Json &parameters);
std::optional<Json> QueryVehicleDetailsState(const std::string &query, const Json &parameters);
/** One shared start per second for text, refit, station coverage and costly control metadata. */
void AdmitWorldTextStart();
/** Resolve one part-local label after static checks; restores vehicle fields before return. */
StringID ResolveVehicleRefitLabelString(const Json &vehicle_handle, const Json &part_handle, uint8_t cargo_id, uint8_t subtype);
std::optional<Json> QueryIndustryDetailsState(const std::string &query, const Json &parameters);
/** Opaque mutation fence only; no order contents, counts or execution state. */
std::string VehicleOrderControlVersion(const Vehicle *vehicle);

} // namespace Integration::Detail

#endif /* INTEGRATION_ACTIONS_INTERNAL_H */
