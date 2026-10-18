/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file observations_vehicles.cpp Bounded, human-visible vehicle details. */

#include "../stdafx.h"
#include "actions_internal.h"
#include "actions_vehicle_scope.h"
#include "../3rdparty/monocypher/monocypher.h"
#include "../cargo_type.h"
#include "../cargotype.h"
#include "../company_func.h"
#include "../core/math_func.hpp"
#include "../core/string_builder.hpp"
#include "../core/utf8.hpp"
#include "../engine_base.h"
#include "../engine_func.h"
#include "../map_func.h"
#include "../misc/endian_buffer.hpp"
#include "../newgrf_engine.h"
#include "../newgrf_text.h"
#include "../rail.h"
#include "../settings_type.h"
#include "../string_func.h"
#include "../strings_func.h"
#include "../timer/timer_game_economy.h"
#include "../timer/timer_game_calendar.h"
#include "../tile_map.h"
#include "../vehicle_base.h"
#include "../vehicle_func.h"
#include "../vehicle_gui.h"

#include <array>
#include <algorithm>
#include <string_view>
#include <unordered_set>
#include <vector>

#include "../table/strings.h"

#include "../safeguards.h"

namespace Integration::Detail {
namespace {

constexpr size_t VEHICLE_REPORT_BYTES = 40 * 1024;
constexpr size_t VEHICLE_PART_SCAN_LIMIT = 4096;
constexpr size_t VEHICLE_PART_PAGE_LIMIT = 16;
constexpr size_t VEHICLE_NAME_BYTES = 256;
constexpr size_t SUBTYPE_LABEL_BYTES = 128;
constexpr size_t VEHICLE_STATUS_BYTES = 1024;

constexpr char LowerHex(char c) { return c >= 'A' && c <= 'F' ? static_cast<char>(c + ('a' - 'A')) : c; }
constexpr int64_t NextPartCursor(size_t part_count, size_t last_emitted)
{
	return last_emitted < part_count && last_emitted + 1 < part_count ? static_cast<int64_t>(last_emitted) : -1;
}

static_assert(LowerHex('A') == 'a' && LowerHex('F') == 'f' && LowerHex('9') == '9');
static_assert(NextPartCursor(2, 0) == 0); // First of two parts, one row per page.
static_assert(NextPartCursor(2, 1) == -1); // Exact end of continuation or larger first page.
static_assert(NextPartCursor(2, 2) == -1); // Empty page past the current end.

/** Action readers remain owner-only; this read models the ordinary public vehicle window. */
const Vehicle *VisiblePrimaryVehicle(const Json &parameters)
{
	const Json &handle = parameters.at("vehicle_id");
	if (!handle.is_object() || handle.size() != 3 || !handle.contains("id") || !handle.contains("generation") || !handle.contains("engine_instance_id") ||
			!handle.at("id").is_number_integer() || !handle.at("generation").is_number_integer() || !handle.at("engine_instance_id").is_string()) {
		throw InvalidParameters("Invalid vehicle handle");
	}
	Parameters fields(handle);
	const uint32_t id = static_cast<uint32_t>(fields.Int("id", 0, VehicleID::Invalid().base() - 1));
	const std::string instance = fields.Text("engine_instance_id", "", 256);
	if (instance.empty() || instance != InstanceId()) throw InvalidParameters("Vehicle target belongs to another engine instance");
	if (!handle.at("generation").is_number_unsigned() && handle.at("generation").get<int64_t>() <= 0) throw InvalidParameters("Invalid vehicle handle");
	const uint64_t generation = handle.at("generation").get<uint64_t>();
	const Vehicle *vehicle = Vehicle::GetIfValid(VehicleID(id));
	if (vehicle == nullptr || vehicle->GetInstanceGeneration() != generation || !IsCompanyBuildableVehicleType(vehicle) || !vehicle->IsPrimaryVehicle()) {
		throw InvalidParameters("Vehicle target is stale or unavailable");
	}
	return vehicle;
}

std::string ShortVehicleName(std::string value)
{
	value = StrMakeValid(std::move(value), StringValidationSetting::AllowNewline);
	std::string plain;
	StringBuilder builder(plain);
	for (char32_t c : Utf8View(value)) if (!IsTextDirectionChar(c)) builder.PutUtf8(c);
	if (plain.size() <= VEHICLE_NAME_BYTES) return plain;
	size_t length = VEHICLE_NAME_BYTES;
	while (length > 0 && (static_cast<uint8_t>(plain[length]) & 0xC0) == 0x80) --length;
	plain.resize(length);
	return plain;
}

Json VehicleStatus(const Vehicle *vehicle)
{
	const auto status = GetVehicleStatusInfo(vehicle, false);
	static constexpr std::array names = {"crashed", "broken_down", "stopping", "no_power", "stopped", "waiting_unbunching",
		"train_stuck", "destination_too_far", "cannot_reach_station", "heading_to_station", "cannot_reach_depot", "heading_to_depot",
		"cannot_reach_waypoint", "heading_to_waypoint", "loading_unloading", "leaving", "no_orders", "other"};
	static_assert(names.size() == static_cast<size_t>(VehicleStatusKind::Other) + 1);
	std::string valid = StrMakeValid(status.text, StringValidationSetting::AllowNewline);
	std::string plain;
	StringBuilder builder(plain);
	for (char32_t c : Utf8View(valid)) if (!IsTextDirectionChar(c)) builder.PutUtf8(c);
	const bool truncated = plain.size() > VEHICLE_STATUS_BYTES;
	if (truncated) {
		size_t length = VEHICLE_STATUS_BYTES;
		while (length > 0 && (static_cast<uint8_t>(plain[length]) & 0xC0) == 0x80) --length;
		plain.resize(length);
	}
	return {{"kind", names[static_cast<size_t>(status.kind)]}, {"text", std::move(plain)}, {"text_truncated", truncated}};
}

Json EnginePurchaseInfo(const Json &parameters)
{
	if (!parameters.is_object() || parameters.size() != 1 || !parameters.contains("engine_id")) throw InvalidParameters("Expected only engine_id");
	Parameters p(parameters);
	const EngineID id(static_cast<uint16_t>(p.Int("engine_id", 0, 63999)));
	const Engine *engine = Engine::GetIfValid(id);
	if (engine == nullptr || !engine->IsEnabled() || !IsEngineBuildable(id, engine->type, _current_company)) throw InvalidParameters("Engine is unavailable");
	Json result = SnapshotBase();
	result["engine_id"] = id.base();
	result["vehicle_type"] = VehicleTypeName(engine->type);
	result["speed_unit"] = "kmh_ish";
	Json performance = Json::object();
	const bool wagon = engine->type == VehicleType::Train && engine->VehInfo<RailVehicleInfo>().railveh_type == RailVehicleType::Wagon;
	if (!wagon) {
		performance["designed_year"] = TimerGameCalendar::ConvertDateToYMD(engine->intro_date).year.base();
		performance["life_calendar_days"] = engine->GetLifeLengthInDays().base();
		performance["reliability_percent"] = ToPercent16(engine->reliability);
	}
	switch (engine->type) {
		case VehicleType::Train: {
			const auto &rail = engine->VehInfo<RailVehicleInfo>();
			performance["empty_weight_tonnes"] = engine->GetDisplayWeight();
			if (!wagon) {
				performance["power_hp"] = engine->GetPower();
				if (_settings_game.vehicle.train_acceleration_model != AccelerationModel::Original) {
					bool exclusively_maglev = true;
					for (RailType type : rail.railtypes) exclusively_maglev &= GetRailTypeInfo(type)->acceleration_type == VehicleAccelerationModel::Maglev;
					if (!exclusively_maglev) performance["maximum_tractive_effort_newtons"] = engine->GetDisplayMaxTractiveEffort();
				}
				if (rail.pow_wag_power != 0) {
					performance["powered_wagon_power_hp"] = rail.pow_wag_power;
					performance["powered_wagon_extra_weight_tonnes"] = rail.pow_wag_weight;
				}
			}
			break;
		}
		case VehicleType::Road:
			if (_settings_game.vehicle.roadveh_acceleration_model != AccelerationModel::Original) {
				performance["empty_weight_tonnes"] = engine->GetDisplayWeight();
				performance["power_hp"] = engine->GetPower();
				performance["maximum_tractive_effort_newtons"] = engine->GetDisplayMaxTractiveEffort();
			}
			break;
		case VehicleType::Ship: {
			const auto &ship = engine->VehInfo<ShipVehicleInfo>();
			const uint speed = engine->GetDisplayMaxSpeed();
			performance["ocean_speed"] = ship.ApplyWaterClassSpeedFrac(speed, true);
			performance["canal_speed"] = ship.ApplyWaterClassSpeedFrac(speed, false);
			break;
		}
		case VehicleType::Aircraft:
			break;
		default:
			throw InvalidParameters("Engine type is unavailable");
	}
	result["performance"] = std::move(performance);
	if (result.dump().size() > 4 * 1024) throw InvalidParameters("Purchase information exceeds the observation byte budget");
	return result;
}

/** Hash every member in order, including pool incarnations and refit structure. */
std::string ConsistVersion(const Vehicle *primary, std::vector<const Vehicle *> &parts)
{
	crypto_blake2b_ctx hash;
	crypto_blake2b_init(&hash, 32);
	auto bytes = [&](std::span<const uint8_t> data) { crypto_blake2b_update(&hash, data.data(), data.size()); };
	auto value = [&](const auto &item) { bytes(EndianBufferWriter<CommandDataBuffer>::FromValue(item)); };
	static constexpr std::string_view DOMAIN = "OpenTTD integration consist v1";
	bytes({reinterpret_cast<const uint8_t *>(DOMAIN.data()), DOMAIN.size()});
	value(std::string(InstanceId()));
	value(primary->index.base());
	value(primary->GetInstanceGeneration());
	std::unordered_set<const Vehicle *> seen;
	for (const Vehicle *part = primary; part != nullptr; part = part->Next()) {
		if (parts.size() == VEHICLE_PART_SCAN_LIMIT) throw InvalidParameters("Vehicle consist exceeds the observation scan budget");
		if (!seen.insert(part).second || part->owner != primary->owner || part->type != primary->type ||
				part->First() != primary || Vehicle::GetIfValid(part->index) != part) {
			throw InvalidParameters("Invalid vehicle consist");
		}
		const uint32_t position = static_cast<uint32_t>(parts.size());
		parts.push_back(part);
		value(position);
		value(part->index.base());
		value(part->GetInstanceGeneration());
		value(part->owner.base());
		value(to_underlying(part->type));
		value(part->engine_type.base());
		value(to_underlying(part->cargo_type));
		value(part->cargo_subtype);
		value(part->cargo_cap);
	}
	value(static_cast<uint32_t>(parts.size()));
	std::array<uint8_t, 32> digest;
	crypto_blake2b_final(&hash, digest.data());
	std::string result = "cv1:" + FormatArrayAsHex(digest);
	std::transform(result.begin() + 4, result.end(), result.begin() + 4, LowerHex);
	return result;
}

Json PartDetail(const Vehicle *part, uint32_t position, bool owned)
{
	const unsigned cargo_id = to_underlying(part->cargo_type);
	const bool carries = part->cargo_cap > 0 && cargo_id < NUM_CARGO && CargoSpec::Get(part->cargo_type)->IsValid();
	Json result = {{"position", position}, {"vehicle_id", Handle(part)}, {"engine_id", part->engine_type.base()},
		{"cargo_id", carries ? Json(cargo_id) : Json(nullptr)}, {"cargo_subtype", carries ? Json(part->cargo_subtype) : Json(nullptr)},
		{"capacity", part->cargo_cap}, {"load", part->cargo.StoredCount()}, {"can_carry_cargo", carries}};
	if (owned) {
		result["refittable_cargo_ids"] = Json::array();
		const Engine *engine = Engine::GetIfValid(part->engine_type);
		if (engine != nullptr) {
			for (CargoType cargo : EnumRange(NUM_CARGO)) {
				if (CargoSpec::Get(cargo)->IsValid() && engine->info.refit_mask.Test(cargo)) result["refittable_cargo_ids"].push_back(to_underlying(cargo));
			}
		}
	}
	return result;
}

Json VehicleDetails(const Json &parameters)
{
	Parameters p(parameters);
	const Vehicle *vehicle = VisiblePrimaryVehicle(parameters);
	const int64_t after = p.Int("after_part_position", -1, VEHICLE_PART_SCAN_LIMIT - 1, -1);
	const size_t limit = static_cast<size_t>(p.Int("part_limit", 1, VEHICLE_PART_PAGE_LIMIT, VEHICLE_PART_PAGE_LIMIT));
	std::string supplied = p.Text("consist_version", "", 68);
	if (!supplied.empty()) {
		if (supplied.size() != 68 || supplied.substr(0, 4) != "cv1:" ||
				!std::ranges::all_of(supplied.begin() + 4, supplied.end(), [](char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); })) {
			throw InvalidParameters("Invalid consist_version");
		}
	}
	if (after >= 0 && supplied.empty()) throw InvalidParameters("Continuation requires consist_version");
	std::vector<const Vehicle *> parts;
	parts.reserve(16);
	const std::string version = ConsistVersion(vehicle, parts);
	if (after >= 0 && supplied != version) throw InvalidParameters("Vehicle consist changed; query its first page again");

	const bool owned = vehicle->owner == _current_company;
	TileIndex location = vehicle->tile;
	if (vehicle->type == VehicleType::Aircraft) {
		location = TileXY(Clamp(vehicle->x_pos / static_cast<int>(TILE_SIZE), 0, static_cast<int>(Map::MaxX())),
			Clamp(vehicle->y_pos / static_cast<int>(TILE_SIZE), 0, static_cast<int>(Map::MaxY())));
	}
	Json result = SnapshotBase();
	result["vehicle_id"] = Handle(vehicle);
	result["owner_company_id"] = vehicle->owner.base();
	result["owned_by_actor"] = owned;
	result["name"] = ShortVehicleName(GetString(STR_VEHICLE_NAME, vehicle->index));
	result["vehicle_type"] = VehicleTypeName(vehicle->type);
	result["engine_id"] = vehicle->engine_type.base();
	result["location"] = IsValidTile(location) ? TileCoordinates(location) : Json(nullptr);
	result["stopped"] = vehicle->vehstatus.Test(VehState::Stopped);
	result["crashed"] = vehicle->vehstatus.Test(VehState::Crashed);
	result["in_depot"] = vehicle->IsInDepot();
	result["status"] = VehicleStatus(vehicle);
	if (owned) {
		bool can_refit = false;
		if (vehicle->IsStoppedInDepot()) {
			const Vehicle *candidate = vehicle;
			do {
				if (IsEngineRefittable(candidate->engine_type)) { can_refit = true; break; }
			} while (candidate->IsGroundVehicle() && (candidate = candidate->Next()) != nullptr);
		}
		result["can_refit_now"] = can_refit;
	}
	result["age_calendar_days"] = vehicle->age.base();
	result["maximum_age_calendar_days"] = vehicle->max_age.base();
	result["build_year"] = vehicle->build_year.base();
	result["reliability_percent"] = ToPercent16(vehicle->reliability);
	result["engine_reliability_percent"] = ToPercent16(vehicle->GetEngine()->reliability);
	result["breakdowns_since_last_service"] = vehicle->breakdowns_since_last_service;
	result["running_cost"] = vehicle->GetDisplayRunningCost().base();
	result["running_cost_unit"] = TimerGameEconomy::UsingWallclockUnits() ? "period" : "year";
	result["profit_this_period"] = vehicle->GetDisplayProfitThisYear().base();
	result["profit_last_period"] = vehicle->GetDisplayProfitLastYear().base();
	result["service_interval"] = vehicle->GetServiceInterval();
	result["service_custom"] = vehicle->ServiceIntervalIsCustom();
	result["service_percent"] = vehicle->ServiceIntervalIsPercent();
	result["last_service_economy_date"] = vehicle->date_of_last_service.base();
	result["order_count"] = vehicle->GetNumOrders();
	result["manual_order_count"] = vehicle->GetNumManualOrders();
	result["current_order_position"] = vehicle->GetNumOrders() == 0 ? Json(nullptr) : Json(vehicle->cur_real_order_index);
	result["part_count"] = parts.size();
	result["consist_version"] = version;
	result["parts"] = Json::array();
	result["next_after_part_position"] = nullptr;
	size_t last_emitted = parts.size();
	for (size_t position = static_cast<size_t>(after + 1); position < parts.size() && result["parts"].size() < limit; ++position) {
		result["parts"].push_back(PartDetail(parts[position], static_cast<uint32_t>(position), owned));
		last_emitted = position;
	}
	const int64_t next = NextPartCursor(parts.size(), last_emitted);
	if (next >= 0) result["next_after_part_position"] = next;
	if (result.dump().size() > VEHICLE_REPORT_BYTES) throw InvalidParameters("Vehicle report exceeds the observation byte budget");
	return result;
}

/** The GUI's refit preview temporarily changes these fields. Cached NewGRF
 * variables may be filled on any relative part, so invalidate the whole
 * bounded consist after restoring the fields, including on exceptions. */
class RefitProbeGuard {
	Vehicle *selected;
	CargoType original_cargo;
	uint8_t original_subtype;
	const std::vector<const Vehicle *> &parts;
public:
	RefitProbeGuard(Vehicle *selected, const std::vector<const Vehicle *> &parts) noexcept :
		selected(selected), original_cargo(selected->cargo_type), original_subtype(selected->cargo_subtype), parts(parts) {}
	RefitProbeGuard(const RefitProbeGuard &) = delete;
	RefitProbeGuard &operator=(const RefitProbeGuard &) = delete;
	~RefitProbeGuard() noexcept
	{
		this->selected->cargo_type = this->original_cargo;
		this->selected->cargo_subtype = this->original_subtype;
		for (const Vehicle *part : this->parts) const_cast<Vehicle *>(part)->InvalidateNewGRFCache();
	}
};

/** Preserve GUI callback value semantics without creating a GUI warning for an
 * invalid result in a read-only request. Raw subtype 255 is reserved for keep. */
StringID ProbeCargoSuffix(Vehicle *part)
{
	std::array<int32_t, 1> regs100{};
	const uint16_t callback = GetVehicleCallback(CBID_VEHICLE_CARGO_SUFFIX, 0, 0, part->engine_type, part, regs100);
	const GRFFile *grf = part->GetGRF();
	if (grf->grf_version < 8 && callback == 0xFF) return STR_EMPTY;
	if (callback == CALLBACK_FAILED || callback == 0x400) return STR_EMPTY;
	if (callback == 0x40F) return GetGRFStringID(part->GetGRFID(), static_cast<GRFStringID>(regs100[0]));
	if (callback > 0x400) return STR_EMPTY;
	return GetGRFStringID(part->GetGRFID(), GRFSTR_MISC_GRF_TEXT + callback);
}

std::pair<std::string, bool> SubtypeLabel(StringID string)
{
	std::string value = StrMakeValid(GetString(string), StringValidationSetting::AllowNewline);
	std::string plain;
	StringBuilder builder(plain);
	for (char32_t c : Utf8View(value)) if (!IsTextDirectionChar(c)) builder.PutUtf8(c);
	if (plain.size() <= SUBTYPE_LABEL_BYTES) return {std::move(plain), false};
	size_t length = SUBTYPE_LABEL_BYTES;
	while (length > 0 && (static_cast<uint8_t>(plain[length]) & 0xC0) == 0x80) --length;
	plain.resize(length);
	return {std::move(plain), true};
}

struct RefitTarget {
	const Vehicle *primary;
	const Vehicle *part;
	std::vector<const Vehicle *> parts;
	CargoType cargo;
	const Engine *engine;
	bool refittable;
};

/** All static privacy, incarnation, membership and cargo checks precede the
 * shared callback admission gate. */
RefitTarget ReadRefitTarget(const Json &vehicle_handle, const Json &part_handle, uint8_t cargo_id)
{
	const Vehicle *primary = VisiblePrimaryVehicle(Json{{"vehicle_id", vehicle_handle}});
	if (primary->owner != _current_company) throw InvalidParameters("Refit subtypes require an owned vehicle");
	if (!part_handle.is_object() || part_handle.size() != 3 || !part_handle.contains("id") || !part_handle.contains("generation") || !part_handle.contains("engine_instance_id") ||
			!part_handle.at("id").is_number_integer() || !part_handle.at("generation").is_number_integer() || !part_handle.at("engine_instance_id").is_string()) {
		throw InvalidParameters("Invalid vehicle part handle");
	}
	Parameters part_fields(part_handle);
	const uint32_t part_id = static_cast<uint32_t>(part_fields.Int("id", 0, VehicleID::Invalid().base() - 1));
	const std::string instance = part_fields.Text("engine_instance_id", "", 256);
	if (instance.empty() || instance != InstanceId()) throw InvalidParameters("Vehicle part belongs to another engine instance");
	if (!part_handle.at("generation").is_number_unsigned() && part_handle.at("generation").get<int64_t>() <= 0) throw InvalidParameters("Invalid vehicle part handle");
	const Vehicle *part = Vehicle::GetIfValid(VehicleID(part_id));
	if (part == nullptr || part->GetInstanceGeneration() != part_handle.at("generation").get<uint64_t>()) throw InvalidParameters("Vehicle part is stale");
	std::vector<const Vehicle *> parts = NativeConsistParts(primary);
	for (size_t position = 0; position < parts.size(); ++position) {
		if (parts[position]->First() != primary || parts[position]->Previous() != (position == 0 ? nullptr : parts[position - 1])) {
			throw InvalidParameters("Vehicle consist links are invalid");
		}
	}
	if (std::ranges::find(parts, part) == parts.end()) throw InvalidParameters("Vehicle part is not in the selected consist");
	const CargoType cargo{cargo_id};
	if (cargo_id >= NUM_CARGO || !CargoSpec::Get(cargo)->IsValid()) throw InvalidParameters("Cargo is unavailable");
	const Engine *engine = Engine::GetIfValid(part->engine_type);
	if (engine == nullptr) throw InvalidParameters("Vehicle part engine is unavailable");
	const bool refittable = engine->CanCarryCargo() && engine->info.refit_mask.Test(cargo);
	return {primary, part, std::move(parts), cargo, engine, refittable};
}

Json VehicleRefitSubtypes(const Json &parameters)
{
	Parameters p(parameters);
	const uint8_t cargo_id = static_cast<uint8_t>(p.Int("cargo_id", 0, NUM_CARGO - 1));
	const int64_t after = p.Int("after_subtype", -1, 254, -1);
	const size_t limit = static_cast<size_t>(p.Int("limit", 1, 16, 16));
	RefitTarget target = ReadRefitTarget(parameters.at("vehicle_id"), parameters.at("part_id"), cargo_id);
	Json result = SnapshotBase();
	result["vehicle_id"] = Handle(target.primary);
	result["part_id"] = Handle(target.part);
	result["cargo_id"] = cargo_id;
	result["refittable"] = target.refittable;
	result["current_cargo"] = target.part->cargo_type == target.cargo;
	result["keep_option"] = target.refittable;
	result["subtypes"] = Json::array();
	result["next_after_subtype"] = nullptr;
	result["complete"] = true;
	result["probe_cap_reached"] = false;
	if (!target.refittable || !target.engine->info.callback_mask.Test(VehicleCallbackMask::CargoSuffix)) return result;
	if (target.part->GetGRF() == nullptr) throw InvalidParameters("Vehicle subtype callback has no GRF");
	AdmitWorldTextStart();

	std::vector<std::pair<uint8_t, StringID>> page;
	page.reserve(limit);
	uint16_t valid_count = 0;
	bool terminated = false;
	{
		Vehicle *mutable_part = const_cast<Vehicle *>(target.part);
		RefitProbeGuard guard(mutable_part, target.parts); // Installed before the first write.
		mutable_part->cargo_type = target.cargo;
		for (uint16_t candidate = 0; candidate < 255; ++candidate) {
			mutable_part->cargo_subtype = static_cast<uint8_t>(candidate);
			mutable_part->First()->InvalidateNewGRFCache();
			mutable_part->InvalidateNewGRFCache();
			const StringID string = ProbeCargoSuffix(mutable_part);
			if (string == STR_EMPTY) { terminated = true; break; }
			valid_count = candidate + 1;
			if (candidate > after && page.size() < limit) page.emplace_back(static_cast<uint8_t>(candidate), string);
		}
	}
	result["probe_cap_reached"] = !terminated;
	for (const auto &[candidate, string] : page) {
		auto [label, truncated] = SubtypeLabel(string);
		result["subtypes"].push_back({{"value", candidate}, {"label", std::move(label)}, {"label_truncated", truncated}});
	}
	if (!page.empty() && page.back().first + 1 < valid_count) {
		result["next_after_subtype"] = page.back().first;
		result["complete"] = false;
	}
	if (result.dump().size() > VEHICLE_REPORT_BYTES) throw InvalidParameters("Refit subtype report exceeds the observation byte budget");
	return result;
}

} // namespace

/** Render one part-local subtype token using the same first-empty boundary as
 * the refit list. The caller can format the returned StringID after this guard
 * has restored cargo state and invalidated the bounded consist caches. */
StringID ResolveVehicleRefitLabelString(const Json &vehicle_handle, const Json &part_handle, uint8_t cargo_id, uint8_t subtype)
{
	if (subtype == UINT8_MAX) throw InvalidParameters("Keep-current subtype has no label");
	RefitTarget target = ReadRefitTarget(vehicle_handle, part_handle, cargo_id);
	if (!target.refittable || !target.engine->info.callback_mask.Test(VehicleCallbackMask::CargoSuffix) || target.part->GetGRF() == nullptr) {
		throw InvalidParameters("Vehicle part has no refit subtype labels");
	}
	AdmitWorldTextStart();
	StringID chosen = STR_EMPTY;
	{
		Vehicle *mutable_part = const_cast<Vehicle *>(target.part);
		RefitProbeGuard guard(mutable_part, target.parts);
		mutable_part->cargo_type = target.cargo;
		for (uint16_t candidate = 0; candidate <= subtype; ++candidate) {
			mutable_part->cargo_subtype = static_cast<uint8_t>(candidate);
			mutable_part->First()->InvalidateNewGRFCache();
			mutable_part->InvalidateNewGRFCache();
			chosen = ProbeCargoSuffix(mutable_part);
			if (chosen == STR_EMPTY) throw InvalidParameters("Vehicle refit subtype is unavailable");
		}
	}
	return chosen;
}

std::optional<Json> QueryVehicleDetailsState(const std::string &query, const Json &parameters)
{
	if (query == "vehicle.details") return VehicleDetails(parameters);
	if (query == "vehicle.refit_subtypes") return VehicleRefitSubtypes(parameters);
	if (query == "engine.purchase_info") return EnginePurchaseInfo(parameters);
	return std::nullopt;
}

} // namespace Integration::Detail
