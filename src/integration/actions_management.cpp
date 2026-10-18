/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License, version 2.
 */

/** @file actions_management.cpp Typed company fleet and group operations. */

#include "../stdafx.h"
#include "actions_internal.h"
#include "actions_vehicle_scope.h"
#include "renderer.h"
#include "renderer_input.h"
#include "../autoreplace_base.h"
#include "../autoreplace_cmd.h"
#include "../company_base.h"
#include "../command_func.h"
#include "../depot_map.h"
#include "../engine_base.h"
#include "../engine_cmd.h"
#include "../engine_func.h"
#include "../group.h"
#include "../group_cmd.h"
#include "../order_func.h"
#include "../rail_map.h"
#include "../settings_cmd.h"
#include "../settings_internal.h"
#include "../train.h"
#include "../train_cmd.h"
#include "../vehicle_base.h"
#include "../vehicle_cmd.h"
#include "../vehicle_func.h"
#include "../3rdparty/monocypher/monocypher.h"

#include <array>
#include <set>

#include "../safeguards.h"

namespace Integration::Detail {
namespace {

GroupID GroupHandle(const Json &parameters, const char *key, bool optional = false)
{
	if (optional && !parameters.contains(key)) return GroupID::Invalid();
	const Json &handle = parameters.at(key);
	if (!handle.is_object() || handle.size() != 3 || !handle.contains("id") || !handle.contains("generation") || !handle.contains("engine_instance_id")) throw InvalidParameters("Invalid group handle");
	if (!handle.at("engine_instance_id").is_string() || handle.at("engine_instance_id").get<std::string>() != InstanceId()) throw InvalidParameters("Group belongs to another engine instance");
	if (!handle.at("id").is_number_integer() || !handle.at("generation").is_number_integer()) throw InvalidParameters("Invalid group handle numbers");
	auto id = handle.at("id").get<uint64_t>();
	auto generation = handle.at("generation").get<uint64_t>();
	if (id >= 64000 || generation == 0) throw InvalidParameters("Invalid group handle range");
	const Group *group = Group::GetIfValid(GroupID(static_cast<uint16_t>(id)));
	if (group == nullptr || group->owner != _current_company || group->GetInstanceGeneration() != generation) throw InvalidParameters("Group is stale or not owned");
	return group->index;
}

VehicleType VehicleKind(const Parameters &p)
{
	auto kind = p.Text("vehicle_type");
	if (kind == "rail") return VehicleType::Train;
	if (kind == "road") return VehicleType::Road;
	if (kind == "ship") return VehicleType::Ship;
	if (kind == "aircraft") return VehicleType::Aircraft;
	throw InvalidParameters("Invalid vehicle type");
}

GroupID Scope(const Json &parameters)
{
	Parameters p(parameters);
	auto scope = p.Text("scope");
	if (scope == "group") return GroupHandle(parameters, "group_id");
	if (parameters.contains("group_id")) throw InvalidParameters("Only group scope accepts group_id");
	if (scope == "all") return ALL_GROUP;
	if (scope == "default") return DEFAULT_GROUP;
	throw InvalidParameters("Invalid group scope");
}

Colours Colour(const Parameters &p)
{
	auto names = CompanyColourNames();
	auto colour = p.Text("colour");
	if (colour == "inherit") return Colours::Invalid;
	auto found = std::ranges::find(names, colour);
	if (found == names.end()) throw InvalidParameters("Invalid colour");
	return static_cast<Colours>(found - names.begin());
}

struct SettingKey { std::string_view public_name; std::string_view native_name; int32_t minimum; int32_t maximum; bool boolean; };
static constexpr std::array<SettingKey, 9> SETTINGS = {{
	{"autorenew_enabled", "company.engine_renew", 0, 1, true},
	{"autorenew_months", "company.engine_renew_months", -12, 12, false},
	{"autorenew_minimum_money", "company.engine_renew_money", 0, 2000000, false},
	{"renew_keep_length", "company.renew_keep_length", 0, 1, true},
	{"service_percent", "company.vehicle.servint_ispercent", 0, 1, true},
	{"service_trains", "company.vehicle.servint_trains", 0, 800, false},
	{"service_road", "company.vehicle.servint_roadveh", 0, 800, false},
	{"service_ships", "company.vehicle.servint_ships", 0, 800, false},
	{"service_aircraft", "company.vehicle.servint_aircraft", 0, 800, false},
}};

/** The command engine accepts a company-prefix shortcut, while the GUI Posts
 * the descriptor's bare name. Admit exactly those two spellings for one of
 * our nine registered settings, never the generic setting-name shortcut. */
const SettingKey *FindSettingKeyAlias(std::string_view name)
{
	static constexpr std::string_view prefix = "company.";
	for (const SettingKey &key : SETTINGS) {
		if (!key.native_name.starts_with(prefix)) continue;
		if (name == key.native_name || name == key.native_name.substr(prefix.size())) return &key;
	}
	return nullptr;
}

bool MatchesNativeDescriptor(const SettingKey &key, const SettingDesc *descriptor)
{
	static constexpr std::string_view prefix = "company.";
	if (descriptor == nullptr || !key.native_name.starts_with(prefix) || !descriptor->IsIntSetting() ||
			!descriptor->flags.Test(SettingFlag::PerCompany)) return false;
	return descriptor->GetName() == key.native_name.substr(prefix.size()) &&
		GetSettingFromName(key.native_name) == descriptor && descriptor->AsIntSetting()->IsBoolSetting() == key.boolean;
}

const SettingKey &Setting(const Parameters &p)
{
	auto key = p.Text("key");
	auto found = std::ranges::find(SETTINGS, key, &SettingKey::public_name);
	if (found == SETTINGS.end()) throw InvalidParameters("Unknown company setting");
	return *found;
}

int32_t SettingValue(const Json &parameters, const SettingKey &key)
{
	const Json &value = parameters.at("value");
	if (key.boolean) {
		if (!value.is_boolean()) throw InvalidParameters("Company setting requires a boolean");
		return value.get<bool>() ? 1 : 0;
	}
	if (!value.is_number_integer()) throw InvalidParameters("Company setting requires an integer");
	auto number = value.get<int64_t>();
	if (number < key.minimum || number > key.maximum) throw InvalidParameters("Company setting is out of range");
	if (key.public_name.starts_with("service_") && key.public_name != "service_percent" && number != 0 &&
		GetServiceIntervalClamped(static_cast<int>(number), Company::Get(_current_company)->settings.vehicle.servint_ispercent) != number)
		throw InvalidParameters("Company service interval is outside the current unit range");
	return static_cast<int32_t>(number);
}

/** Fixed-size private proof of a sorted stream; no public content is emitted. */
class ProofHash {
	crypto_blake2b_ctx context{};
	uint64_t records = 0;
	bool include_incarnations;
public:
	explicit ProofHash(bool include_incarnations) : include_incarnations(include_incarnations)
	{
		crypto_blake2b_init(&this->context, 32);
		this->Add(Json("integration.management/v1"));
	}
	bool IncludeIncarnations() const { return this->include_incarnations; }
	void Add(const Json &item)
	{
		std::string bytes = item.dump();
		uint64_t length = bytes.size();
		uint8_t prefix[8];
		for (uint i = 0; i < 8; ++i) prefix[i] = static_cast<uint8_t>(length >> (8 * i));
		crypto_blake2b_update(&this->context, prefix, sizeof(prefix));
		crypto_blake2b_update(&this->context, reinterpret_cast<const uint8_t *>(bytes.data()), bytes.size());
		++this->records;
	}
	Json Finish()
	{
		this->Add(Json{{"records", this->records}});
		std::array<uint8_t, 32> bytes{};
		crypto_blake2b_final(&this->context, bytes.data());
		static constexpr char digits[] = "0123456789abcdef";
		std::string hex; hex.reserve(bytes.size() * 2);
		for (uint8_t byte : bytes) { hex.push_back(digits[byte >> 4]); hex.push_back(digits[byte & 15]); }
		return Json{{"hash", hex}, {"records", this->records - 1}};
	}
};

void AddCompany(ProofHash &proof)
{
	const Company *company = Company::Get(_current_company);
	Json item = {{"company", company->index.base()}};
	if (proof.IncludeIncarnations()) item["generation"] = company->GetInstanceGeneration();
	proof.Add(item);
}

void AddGroup(ProofHash &proof, const Group *group, bool livery = false)
{
	Json item = {{"group", group->index.base()}, {"owner", group->owner.base()}, {"parent", group->parent.base()}, {"type", to_underlying(group->vehicle_type)}, {"flags", group->flags.base()}};
	if (proof.IncludeIncarnations()) item["generation"] = group->GetInstanceGeneration();
	if (livery) item["livery"] = {group->livery.colour1, group->livery.colour2, group->livery.in_use.base()};
	proof.Add(item);
}

std::set<GroupID> GroupClosure(GroupID id)
{
	std::set<GroupID> closure;
	std::vector<GroupID> pending{id};
	while (!pending.empty()) {
		GroupID next = pending.back(); pending.pop_back();
		if (!closure.insert(next).second) continue;
		const Group *group = Group::GetIfValid(next);
		if (group == nullptr || group->owner != _current_company) throw InvalidParameters("Group hierarchy changed");
		for (GroupID child : group->children) pending.push_back(child);
	}
	return closure;
}

void AddClosure(ProofHash &proof, GroupID root, bool vehicles, bool rules, bool livery)
{
	auto closure = GroupClosure(root);
	proof.Add({{"closure_count", closure.size()}});
	for (GroupID id : closure) AddGroup(proof, Group::Get(id), livery);
	if (vehicles) for (const Vehicle *vehicle : Vehicle::Iterate()) {
		if (vehicle->owner == _current_company && vehicle->IsPrimaryVehicle() && closure.contains(vehicle->group_id))
		{
			Json item = {{"vehicle", vehicle->index.base()}, {"group", vehicle->group_id.base()}};
			if (proof.IncludeIncarnations()) item["generation"] = vehicle->GetInstanceGeneration();
			proof.Add(item);
		}
	}
	if (rules) {
		std::multiset<std::tuple<uint16_t, uint16_t, uint16_t, bool>> logical_rules;
		for (const EngineRenew *rule : EngineRenew::Iterate()) if (closure.contains(rule->group_id))
			logical_rules.emplace(rule->group_id.base(), rule->from.base(), rule->to.base(), rule->replace_when_old);
		proof.Add({{"logical_rule_count", logical_rules.size()}});
		for (const auto &[group, from, to, when_old] : logical_rules)
			proof.Add({{"group", group}, {"from", from}, {"to", to}, {"when_old", when_old}});
	}
}

void AddInheritedLivery(ProofHash &proof, GroupID group)
{
	std::set<GroupID> seen;
	for (GroupID parent = Group::Get(group)->parent; parent != GroupID::Invalid();) {
		if (!seen.insert(parent).second) throw InvalidParameters("Group hierarchy is cyclic");
		const Group *ancestor = Group::GetIfValid(parent);
		if (ancestor == nullptr || ancestor->owner != _current_company) throw InvalidParameters("Group ancestor changed");
		AddGroup(proof, ancestor, true);
		parent = ancestor->parent;
	}
	const Company *company = Company::Get(_current_company);
	proof.Add({{"default_livery", {to_underlying(company->livery[LiveryScheme::Default].colour1), to_underlying(company->livery[LiveryScheme::Default].colour2)}}});
}

void AddVehicleMembership(ProofHash &proof, const Vehicle *vehicle, bool chain)
{
	Json item = {{"vehicle", vehicle->index.base()}, {"owner", vehicle->owner.base()}, {"group", vehicle->group_id.base()}, {"type", to_underlying(vehicle->type)}, {"primary", vehicle->IsPrimaryVehicle()}};
	if (proof.IncludeIncarnations()) item["generation"] = vehicle->GetInstanceGeneration();
	proof.Add(item);
	if (chain) for (const Vehicle *part : NativeConsistParts(vehicle)) {
		Json member = {{"member", part->index.base()}, {"owner", part->owner.base()}, {"group", part->group_id.base()}};
		if (proof.IncludeIncarnations()) member["generation"] = part->GetInstanceGeneration();
		proof.Add(member);
	}
}

void AddUnitFlipState(ProofHash &proof, const Vehicle *vehicle)
{
	const Train *selected = Train::From(vehicle);
	const Train *front = Train::From(vehicle->First());
	AddVehicleMembership(proof, vehicle, true);
	Json state = {{"selected_flipped", selected->flags.Test(VehicleRailFlag::Flipped)}, {"selected_multiheaded", selected->IsMultiheaded()},
		{"selected_articulated_engine", EngInfo(selected->engine_type)->callback_mask.Test(VehicleCallbackMask::ArticEngine)},
		{"front", front->index.base()}, {"front_stopped_in_depot", front->IsStoppedInDepot()}};
	if (proof.IncludeIncarnations()) state["front_generation"] = front->GetInstanceGeneration();
	proof.Add(state);
}

void AddAutoreplaceEligibility(ProofHash &proof, const Vehicle *vehicle)
{
	const Vehicle *front = vehicle->First();
	Json selected = {{"selected", vehicle->index.base()},
		{"selected_crashed", vehicle->vehstatus.Test(VehState::Crashed)}, {"chain_in_depot", vehicle->IsChainInDepot()},
		{"front", front->index.base()}, {"front_stopped_in_depot", front->IsStoppedInDepot()}};
	if (proof.IncludeIncarnations()) {
		selected["selected_generation"] = vehicle->GetInstanceGeneration();
		selected["front_generation"] = front->GetInstanceGeneration();
	}
	proof.Add(selected);
	if (vehicle->type == VehicleType::Train) {
		const Train *selected = Train::From(vehicle);
		proof.Add({{"selected_front_engine", selected->IsFrontEngine()}, {"selected_articulated", selected->IsArticulatedPart()},
			{"selected_rear_dualheaded", selected->IsRearDualheaded()}});
	}
	for (const Vehicle *part : NativeConsistParts(front)) {
		Json item = {{"part", part->index.base()}, {"owner", part->owner.base()},
			{"tile", part->tile.base()}, {"depot_tile", IsDepotTile(part->tile)}, {"engine", part->engine_type.base()},
			{"cargo", to_underlying(part->cargo_type)}, {"group", part->group_id.base()}, {"crashed", part->vehstatus.Test(VehState::Crashed)}};
		if (proof.IncludeIncarnations()) item["generation"] = part->GetInstanceGeneration();
		proof.Add(item);
	}
}

TrainForceProceeding NextForceMode(const Train *train)
{
	/* Mirror the small decision in CmdForceTrainProceed, so the proof pins
	 * the actual toggle outcome without fencing unrelated world changes. */
	if (train->vehstatus.Test(VehState::Crashed) || train->force_proceed == TFP_SIGNAL) return TFP_NONE;
	if (!train->flags.Test(VehicleRailFlag::Stuck)) return train->IsChainInDepot() ? TFP_STUCK : TFP_SIGNAL;
	const Train *front = train->GetMovingFront();
	TileIndex next = TileAddByDiagDir(front->tile, TrackdirToExitdir(front->GetVehicleTrackdir()));
	if (next == INVALID_TILE || !IsTileType(next, TileType::Railway) || !HasSignals(next)) return TFP_STUCK;
	TrackBits tracks = DiagdirReachesTracks(TrackdirToExitdir(front->GetVehicleTrackdir())) & GetTrackBits(next);
	return tracks.Any() && HasSignalOnTrack(next, FindFirstTrack(tracks)) ? TFP_SIGNAL : TFP_STUCK;
}

void AddRule(ProofHash &proof, GroupID group, EngineID old_engine)
{
	const Company *company = Company::Get(_current_company);
	bool found = false;
	for (const EngineRenew *rule = company->engine_renew_list; rule != nullptr; rule = rule->next) {
		if (rule->group_id != group || rule->from != old_engine) continue;
		proof.Add({{"group", group.base()}, {"from", rule->from.base()}, {"to", rule->to.base()}, {"when_old", rule->replace_when_old}});
		found = true;
	}
	if (!found) proof.Add({{"rule_absent", true}, {"group", group.base()}, {"from", old_engine.base()}});
}

void AddReplacementContext(ProofHash &proof, GroupID group)
{
	std::set<GroupID> seen;
	while (Group::IsValidID(group)) {
		if (!seen.insert(group).second) throw InvalidParameters("Replacement group hierarchy is cyclic");
		const Group *current = Group::GetIfValid(group);
		if (current == nullptr || current->owner != _current_company) throw InvalidParameters("Replacement group changed");
		AddGroup(proof, current);
		group = current->parent;
	}
	proof.Add({{"replacement_group_end", group.base()}});
}

void AddRelevantRuleStream(ProofHash &proof, const std::map<EngineID, std::set<GroupID>> &cohorts)
{
	const Company *company = Company::Get(_current_company);
	for (const auto &[engine, groups] : cohorts) for (GroupID group : groups) {
		proof.Add({{"replacement_engine", engine.base()}, {"vehicle_group", group.base()}});
		uint direct_count = 0, fallback_count = 0;
		/* Search order matters. Pin only rules applicable to this selected
		 * cohort, then ALL_GROUP if native fallback can actually be reached. */
		for (const EngineRenew *rule = company->engine_renew_list; rule != nullptr; rule = rule->next) {
			if (rule->from != engine || !GroupIsInGroup(group, rule->group_id)) continue;
			const Engine *replacement = Engine::GetIfValid(rule->to);
			Json item = {{"group", rule->group_id.base()},
				{"from", rule->from.base()}, {"to", rule->to.base()}, {"when_old", rule->replace_when_old},
				{"replacement_buildable", replacement != nullptr && IsEngineBuildable(rule->to, replacement->type, _current_company)}};
			proof.Add(item);
			++direct_count;
		}
		bool fallback = group == DEFAULT_GROUP || (Group::IsValidID(group) && !Group::Get(group)->flags.Test(GroupFlag::ReplaceProtection));
		if (direct_count == 0 && fallback) for (const EngineRenew *rule = company->engine_renew_list; rule != nullptr; rule = rule->next) {
			if (rule->from != engine || rule->group_id != ALL_GROUP) continue;
			const Engine *replacement = Engine::GetIfValid(rule->to);
			Json item = {{"fallback", true}, {"group", rule->group_id.base()}, {"from", rule->from.base()},
				{"to", rule->to.base()}, {"when_old", rule->replace_when_old},
				{"replacement_buildable", replacement != nullptr && IsEngineBuildable(rule->to, replacement->type, _current_company)}};
			proof.Add(item);
			++fallback_count;
		}
		proof.Add({{"direct_count", direct_count}, {"fallback_count", fallback_count}, {"fallback_allowed", fallback}});
	}
}

} // namespace

/** Read-only permission for the actual SettingEntry descriptor resolved by the
 * native GameOptions row helper. The registry above is the only key list. */
bool IsAllowedCompanySetting(const SettingDesc *descriptor)
{
	for (const SettingKey &key : SETTINGS) if (MatchesNativeDescriptor(key, descriptor)) return true;
	return false;
}

std::optional<ActionResult> DispatchManagementAction(const std::string &action, const Json &parameters, ActionInvocation &invocation)
{
	Parameters p(parameters);
	if (action == "group.create") return invocation.Post<Commands::CreateGroup>(VehicleKind(p), GroupHandle(parameters, "parent_group_id", true));
	if (action == "group.delete") return invocation.Post<Commands::DeleteGroup>(GroupHandle(parameters, "group_id"));
	if (action == "group.rename") return invocation.Post<Commands::AlterGroup>(AlterGroupMode::Rename, GroupHandle(parameters, "group_id"), GroupID::Invalid(), p.Text("name"));
	if (action == "group.parent.set") return invocation.Post<Commands::AlterGroup>(AlterGroupMode::SetParent, GroupHandle(parameters, "group_id"), GroupHandle(parameters, "parent_group_id", true), std::string{});
	if (action == "group.vehicle.move") {
		VehicleID id = p.Vehicle();
		if (!Vehicle::Get(id)->IsPrimaryVehicle()) throw InvalidParameters("Group move requires a primary vehicle");
		return invocation.Post<Commands::AddVehicleToGroup>(parameters.contains("group_id") ? GroupHandle(parameters, "group_id") : DEFAULT_GROUP, id, p.Bool("include_shared"), VehicleListIdentifier{});
	}
	if (action == "group.protection.set" || action == "group.wagon_removal.set") return invocation.Post<Commands::SetGroupFlag>(GroupHandle(parameters, "group_id"), action == "group.protection.set" ? GroupFlag::ReplaceProtection : GroupFlag::ReplaceWagonRemoval, p.Bool("enabled"), p.Bool("recursive"));
	if (action == "group.livery.set") {
		auto channel = p.Text("channel");
		if (channel != "primary" && channel != "secondary") throw InvalidParameters("Invalid livery channel");
		return invocation.Post<Commands::SetGroupLivery>(GroupHandle(parameters, "group_id"), channel == "primary", Colour(p));
	}
	if (action == "autoreplace.set" || action == "autoreplace.remove") {
		GroupID scope = Scope(parameters);
		EngineID old_engine(p.Int("old_engine_id", 0, 63999));
		EngineID new_engine = action == "autoreplace.set" ? EngineID(p.Int("new_engine_id", 0, 63999)) : EngineID::Invalid();
		return invocation.Post<Commands::SetAutoreplace>(scope, old_engine, new_engine, action == "autoreplace.set" && p.Bool("when_old"));
	}
	if (action == "vehicle.service_interval.set") {
		VehicleID id = p.Vehicle();
		if (!Vehicle::Get(id)->IsPrimaryVehicle()) throw InvalidParameters("Service interval requires a primary vehicle");
		auto mode = p.Text("mode");
		if (mode != "default" && mode != "custom") throw InvalidParameters("Invalid service interval mode");
		bool custom = mode == "custom";
		if (custom != parameters.contains("interval") || custom != parameters.contains("percent")) throw InvalidParameters("Service interval parameters do not match mode");
		int64_t interval = custom ? p.Int("interval", 1, UINT16_MAX) : 0;
		bool percent = custom && p.Bool("percent");
		if (custom && GetServiceIntervalClamped(static_cast<int>(interval), percent) != interval) throw InvalidParameters("Vehicle service interval is outside the current unit range");
		return invocation.Post<Commands::ChangeServiceInterval>(id, interval, custom, percent);
	}
	if (action == "train.force_proceed") {
		VehicleID id = p.Vehicle();
		if (Vehicle::Get(id)->type != VehicleType::Train || !Vehicle::Get(id)->IsPrimaryVehicle()) throw InvalidParameters("Force proceed requires a primary train");
		return invocation.Post<Commands::ForceTrainProceed>(TileIndex{0}, id);
	}
	if (action == "train.unit.flip") {
		VehicleID id = p.Vehicle();
		if (Vehicle::Get(id)->type != VehicleType::Train) throw InvalidParameters("Unit flip requires a train unit");
		return invocation.Post<Commands::ReverseTrainDirection>(TileIndex{0}, id, true);
	}
	if (action == "vehicle.autoreplace") return invocation.Post<Commands::AutoreplaceVehicle>(p.Vehicle());
	if (action == "catalog.engine.visibility.set") return invocation.Post<Commands::SetVehicleVisibility>(EngineID(p.Int("engine_id", 0, 63999)), p.Bool("hidden"));
	if (action == "company.setting.set") {
		const SettingKey &key = Setting(p);
		return invocation.Post<Commands::ChangeCompanySetting>(std::string(key.native_name), SettingValue(parameters, key));
	}
	return std::nullopt;
}

std::optional<Json> QueryManagementState(const std::string &query, const Json &parameters)
{
	Parameters p(parameters);
	if (query == "groups") {
		Json result = SnapshotBase();
		result.update(PoolPage<Group>(p, [](const Group *group) { return group->owner == _current_company; }, [](const Group *group) {
			Json item = {{"group_id", Handle(group)}, {"name", group->name}, {"vehicle_type", VehicleTypeName(group->vehicle_type)},
				{"replace_protection", group->flags.Test(GroupFlag::ReplaceProtection)}, {"wagon_removal", group->flags.Test(GroupFlag::ReplaceWagonRemoval)},
				{"vehicle_count", group->statistics.num_vehicle}, {"profit_last_year", group->statistics.profit_last_year.base()},
				{"livery", {{"primary", to_underlying(group->livery.colour1)}, {"secondary", to_underlying(group->livery.colour2)},
					{"primary_override", group->livery.in_use.Test(Livery::Flag::Primary)}, {"secondary_override", group->livery.in_use.Test(Livery::Flag::Secondary)}}}};
			if (const Group *parent = Group::GetIfValid(group->parent); parent != nullptr) item["parent_group_id"] = Handle(parent);
			return item;
		}));
		return result;
	}
	if (query == "autoreplace") {
		GroupID scope = Scope(parameters);
		const Company *company = Company::Get(_current_company);
		std::set<EngineRenewID> owned;
		for (const EngineRenew *rule = company->engine_renew_list; rule != nullptr; rule = rule->next) owned.insert(rule->index);
		Json result = SnapshotBase();
		result["scope"] = p.Text("scope");
		if (Group::IsValidID(scope)) result["group_id"] = Handle(Group::Get(scope));
		result.update(PoolPage<EngineRenew>(p, [&](const EngineRenew *rule) { return owned.contains(rule->index) && rule->group_id == scope; }, [](const EngineRenew *rule) {
			return Json{{"old_engine_id", rule->from.base()}, {"new_engine_id", rule->to.base()}, {"when_old", rule->replace_when_old}};
		}));
		return result;
	}
	if (query == "group.stats") {
		GroupID scope = Scope(parameters);
		VehicleType type = Group::IsValidID(scope) ? Group::Get(scope)->vehicle_type : VehicleKind(p);
		if (!IsCompanyBuildableVehicleType(type)) throw InvalidParameters("Invalid vehicle type");
		Json result = SnapshotBase();
		result["scope"] = p.Text("scope");
		if (Group::IsValidID(scope)) result["group_id"] = Handle(Group::Get(scope));
		uint64_t count = 0;
		Money this_year = 0, last_year = 0;
		uint64_t engine_count = 0;
		EngineID engine = parameters.contains("engine_id") ? EngineID(p.Int("engine_id", 0, 63999)) : EngineID::Invalid();
		for (const Vehicle *vehicle : Vehicle::Iterate()) {
			if (vehicle->owner != _current_company || vehicle->type != type || !vehicle->IsPrimaryVehicle()) continue;
			if (scope != ALL_GROUP && vehicle->group_id != scope) continue;
			++count;
			this_year += vehicle->GetDisplayProfitThisYear();
			last_year += vehicle->GetDisplayProfitLastYear();
			if (engine != EngineID::Invalid()) for (const Vehicle *part : NativeConsistParts(vehicle)) if (part->engine_type == engine) ++engine_count;
		}
		result["vehicle_count"] = count;
		result["profit_this_year"] = this_year.base();
		result["profit_last_year"] = last_year.base();
		if (engine != EngineID::Invalid()) result["engine_count"] = engine_count;
		return result;
	}
	return std::nullopt;
}

static std::optional<Json> BuildManagementTargets(const std::string &action, const Json &parameters, bool include_incarnations)
{
	static constexpr std::array<std::string_view, 16> names = {"group.create", "group.delete", "group.rename", "group.parent.set", "group.vehicle.move", "group.protection.set", "group.wagon_removal.set", "group.livery.set", "autoreplace.set", "autoreplace.remove", "vehicle.service_interval.set", "train.force_proceed", "train.unit.flip", "vehicle.autoreplace", "catalog.engine.visibility.set", "company.setting.set"};
	if (std::ranges::find(names, action) == names.end()) return std::nullopt;
	RequireCompany();
	Parameters p(parameters);
	ProofHash proof(include_incarnations);
	proof.Add({{"action", action}});
	AddCompany(proof);
	if (action == "group.create") {
		GroupID parent = GroupHandle(parameters, "parent_group_id", true);
		if (parent != GroupID::Invalid()) AddGroup(proof, Group::Get(parent), true);
		else {
			const Company *company = Company::Get(_current_company);
			proof.Add({{"default_livery", {to_underlying(company->livery[LiveryScheme::Default].colour1), to_underlying(company->livery[LiveryScheme::Default].colour2)}}, {"renew_keep_length", company->settings.renew_keep_length}});
		}
	} else if (action == "group.delete") {
		AddClosure(proof, GroupHandle(parameters, "group_id"), true, true, true);
	} else if (action == "group.parent.set") {
		GroupID group = GroupHandle(parameters, "group_id");
		AddClosure(proof, group, false, false, true);
		AddInheritedLivery(proof, group);
		GroupID parent = GroupHandle(parameters, "parent_group_id", true);
		std::set<GroupID> seen;
		while (parent != GroupID::Invalid()) {
			if (!seen.insert(parent).second) throw InvalidParameters("Group hierarchy is cyclic");
			const Group *ancestor = Group::GetIfValid(parent);
			if (ancestor == nullptr || ancestor->owner != _current_company) throw InvalidParameters("Group parent changed");
			AddGroup(proof, ancestor, true);
			parent = ancestor->parent;
		}
	} else if (action == "group.livery.set" || ((action == "group.protection.set" || action == "group.wagon_removal.set") && p.Bool("recursive"))) {
		GroupID group = GroupHandle(parameters, "group_id");
		AddClosure(proof, group, false, false, action == "group.livery.set");
		if (action == "group.livery.set") AddInheritedLivery(proof, group);
	} else if (action == "group.rename" || action == "group.protection.set" || action == "group.wagon_removal.set") {
		AddGroup(proof, Group::Get(GroupHandle(parameters, "group_id")), false);
	} else if (action == "group.vehicle.move") {
		VehicleID id = p.Vehicle();
		const Vehicle *vehicle = Vehicle::Get(id);
		if (!vehicle->IsPrimaryVehicle()) throw InvalidParameters("Group move requires a primary vehicle");
		AddVehicleMembership(proof, vehicle, true);
		if (parameters.contains("group_id")) AddGroup(proof, Group::Get(GroupHandle(parameters, "group_id")));
		if (p.Bool("include_shared")) {
			for (const Vehicle *shared : NativeSharedVehicles(vehicle)) AddVehicleMembership(proof, shared, true);
		}
	} else if (action == "autoreplace.set" || action == "autoreplace.remove") {
		GroupID scope = Scope(parameters);
		if (Group::IsValidID(scope)) AddGroup(proof, Group::Get(scope));
		EngineID old_engine(p.Int("old_engine_id", 0, 63999));
		const Engine *old = Engine::GetIfValid(old_engine);
		if (old == nullptr) throw InvalidParameters("Invalid old engine");
		Json old_state = {{"old_engine", old_engine.base()}, {"enabled", old->IsEnabled()}};
		if (proof.IncludeIncarnations()) old_state["generation"] = old->GetInstanceGeneration();
		proof.Add(old_state);
		if (action == "autoreplace.set") {
			EngineID new_engine(p.Int("new_engine_id", 0, 63999));
			const Engine *replacement = Engine::GetIfValid(new_engine);
			if (replacement == nullptr) throw InvalidParameters("Invalid replacement engine");
			Json new_state = {{"new_engine", new_engine.base()}, {"enabled", replacement->IsEnabled()}, {"buildable", IsEngineBuildable(new_engine, replacement->type, _current_company)}};
			if (proof.IncludeIncarnations()) new_state["generation"] = replacement->GetInstanceGeneration();
			proof.Add(new_state);
		}
		AddRule(proof, scope, old_engine);
	} else if (action == "vehicle.service_interval.set" || action == "train.force_proceed" || action == "train.unit.flip" || action == "vehicle.autoreplace") {
		const Vehicle *vehicle = Vehicle::Get(p.Vehicle());
		if ((action == "vehicle.service_interval.set" || action == "train.force_proceed") && !vehicle->IsPrimaryVehicle()) throw InvalidParameters("Management action requires a primary vehicle");
		if ((action == "train.force_proceed" || action == "train.unit.flip") && vehicle->type != VehicleType::Train) throw InvalidParameters("Management action requires a train");
		if (action == "vehicle.service_interval.set" || action == "train.force_proceed") AddVehicleMembership(proof, vehicle, false);
		else if (action == "train.unit.flip") AddUnitFlipState(proof, vehicle);
		else AddAutoreplaceEligibility(proof, vehicle);
		if (action == "train.force_proceed") {
			const Train *train = Train::From(vehicle);
			proof.Add({{"force_proceed", train->force_proceed}, {"next_force_mode", NextForceMode(train)}});
		}
		if (action == "vehicle.service_interval.set") {
			proof.Add({{"service_interval", vehicle->GetServiceInterval()}, {"custom", vehicle->ServiceIntervalIsCustom()}, {"percent", vehicle->ServiceIntervalIsPercent()}});
			if (p.Text("mode") == "default") {
				const auto &policy = Company::Get(_current_company)->settings.vehicle;
				uint16_t selected = 0;
				switch (vehicle->type) {
					case VehicleType::Train: selected = policy.servint_trains; break;
					case VehicleType::Road: selected = policy.servint_roadveh; break;
					case VehicleType::Ship: selected = policy.servint_ships; break;
					case VehicleType::Aircraft: selected = policy.servint_aircraft; break;
					default: throw InvalidParameters("Unsupported service vehicle type");
				}
				proof.Add({{"default_service_type", to_underlying(vehicle->type)}, {"default_service_interval", selected}, {"default_service_percent", policy.servint_ispercent}});
			}
		}
		if (action == "vehicle.autoreplace") {
			GroupID group = vehicle->group_id;
			AddReplacementContext(proof, group);
			const Company *company = Company::Get(_current_company);
			proof.Add({{"renew", company->settings.engine_renew}, {"months", company->settings.engine_renew_months}, {"money", company->settings.engine_renew_money}, {"keep_length", company->settings.renew_keep_length}});
			std::map<EngineID, std::set<GroupID>> cohorts;
			for (const Vehicle *part : NativeConsistParts(vehicle)) {
				cohorts[part->engine_type].insert(part->group_id);
				if (part->group_id != group) AddReplacementContext(proof, part->group_id);
			}
			AddRelevantRuleStream(proof, cohorts);
		}
	} else if (action == "catalog.engine.visibility.set") {
		EngineID id(p.Int("engine_id", 0, 63999));
		const Engine *engine = Engine::GetIfValid(id);
		if (engine == nullptr) throw InvalidParameters("Invalid engine");
		Json engine_state = {{"engine", id.base()}, {"enabled", engine->IsEnabled()},
			{"buildable", IsEngineBuildable(id, engine->type, _current_company)}, {"hidden", engine->company_hidden.Test(_current_company)}};
		if (proof.IncludeIncarnations()) engine_state["generation"] = engine->GetInstanceGeneration();
		proof.Add(engine_state);
	} else if (action == "company.setting.set") {
		const SettingKey &key = Setting(p);
		const auto &settings = Company::Get(_current_company)->settings;
		int64_t current = 0;
		if (key.public_name == "autorenew_enabled") current = settings.engine_renew;
		else if (key.public_name == "autorenew_months") current = settings.engine_renew_months;
		else if (key.public_name == "autorenew_minimum_money") current = settings.engine_renew_money;
		else if (key.public_name == "renew_keep_length") current = settings.renew_keep_length;
		else if (key.public_name == "service_percent") current = settings.vehicle.servint_ispercent;
		else if (key.public_name == "service_trains") current = settings.vehicle.servint_trains;
		else if (key.public_name == "service_road") current = settings.vehicle.servint_roadveh;
		else if (key.public_name == "service_ships") current = settings.vehicle.servint_ships;
		else if (key.public_name == "service_aircraft") current = settings.vehicle.servint_aircraft;
		proof.Add({{"setting", key.public_name}, {"current", current}, {"service_percent", settings.vehicle.servint_ispercent}});
	}
	return Json::array({Json{{"kind", "management"}, {"state", proof.Finish()}}});
}

std::optional<Json> CaptureManagementTargets(const std::string &action, const Json &parameters)
{
	return BuildManagementTargets(action, parameters, true);
}

namespace {
template <Commands Tcommand>
typename CommandTraits<Tcommand>::Args Decode(const CommandDataBuffer &data)
{
	if (data.size() > 8192) throw InvalidParameters("Native management command is oversized");
	using Arguments = typename CommandTraits<Tcommand>::Args;
	auto arguments = EndianBufferReader::ToValue<Arguments>(data);
	if (EndianBufferWriter<CommandDataBuffer>::FromValue(arguments) != data) throw InvalidParameters("Native management command is noncanonical");
	return arguments;
}

Json NativeGroup(GroupID id)
{
	const Group *group = Group::GetIfValid(id);
	if (group == nullptr || group->owner != _current_company) throw InvalidParameters("Native group is unavailable");
	return Handle(group);
}

Json NativeVehicle(VehicleID id)
{
	const Vehicle *vehicle = Vehicle::GetIfValid(id);
	if (vehicle == nullptr || vehicle->owner != _current_company) throw InvalidParameters("Native vehicle is unavailable");
	return Handle(vehicle);
}

std::string NativeColour(Colours colour)
{
	auto names = CompanyColourNames();
	if (colour == Colours::Invalid) return "inherit";
	if (colour >= Colours::End) throw InvalidParameters("Native colour is invalid");
	return std::string(names[to_underlying(colour)]);
}

void NativeScope(Json &parameters, GroupID group)
{
	if (group == ALL_GROUP) parameters["scope"] = "all";
	else if (group == DEFAULT_GROUP) parameters["scope"] = "default";
	else { parameters["scope"] = "group"; parameters["group_id"] = NativeGroup(group); }
}
} // namespace

std::optional<Json> ManagementIntentState(Commands command, const CommandDataBuffer &data)
{
	Json parameters = Json::object();
	std::string action;
	switch (command) {
		case Commands::CreateGroup: {
			auto [type, parent] = Decode<Commands::CreateGroup>(data);
			parameters["vehicle_type"] = VehicleTypeName(type);
			if (parent != GroupID::Invalid()) parameters["parent_group_id"] = NativeGroup(parent);
			action = "group.create";
			break;
		}
		case Commands::DeleteGroup: {
			auto [group] = Decode<Commands::DeleteGroup>(data);
			parameters["group_id"] = NativeGroup(group);
			action = "group.delete";
			break;
		}
		case Commands::AlterGroup: {
			auto [mode, group, parent, name] = Decode<Commands::AlterGroup>(data);
			parameters["group_id"] = NativeGroup(group);
			if (mode == AlterGroupMode::Rename) { parameters["name"] = name; action = "group.rename"; }
			else if (mode == AlterGroupMode::SetParent) {
				if (parent != GroupID::Invalid()) parameters["parent_group_id"] = NativeGroup(parent);
				action = "group.parent.set";
			} else throw InvalidParameters("Native group alteration is invalid");
			break;
		}
		case Commands::AddVehicleToGroup: {
			auto [group, vehicle, shared, list] = Decode<Commands::AddVehicleToGroup>(data);
			if (vehicle == VehicleID::Invalid() || group == NEW_GROUP) throw InvalidParameters("Native bulk or implicit group move is unavailable");
			parameters["vehicle_id"] = NativeVehicle(vehicle);
			if (group != DEFAULT_GROUP) parameters["group_id"] = NativeGroup(group);
			parameters["include_shared"] = shared;
			action = "group.vehicle.move";
			break;
		}
		case Commands::SetGroupFlag: {
			auto [group, flag, enabled, recursive] = Decode<Commands::SetGroupFlag>(data);
			parameters["group_id"] = NativeGroup(group);
			parameters["enabled"] = enabled;
			parameters["recursive"] = recursive;
			if (flag == GroupFlag::ReplaceProtection) action = "group.protection.set";
			else if (flag == GroupFlag::ReplaceWagonRemoval) action = "group.wagon_removal.set";
			else throw InvalidParameters("Native group flag is unavailable");
			break;
		}
		case Commands::SetGroupLivery: {
			auto [group, primary, colour] = Decode<Commands::SetGroupLivery>(data);
			parameters = {{"group_id", NativeGroup(group)}, {"channel", primary ? "primary" : "secondary"}, {"colour", NativeColour(colour)}};
			action = "group.livery.set";
			break;
		}
		case Commands::SetAutoreplace: {
			auto [group, old_engine, new_engine, when_old] = Decode<Commands::SetAutoreplace>(data);
			NativeScope(parameters, group);
			parameters["old_engine_id"] = old_engine.base();
			if (new_engine == EngineID::Invalid()) action = "autoreplace.remove";
			else { parameters["new_engine_id"] = new_engine.base(); parameters["when_old"] = when_old; action = "autoreplace.set"; }
			break;
		}
		case Commands::ChangeServiceInterval: {
			auto [vehicle, interval, custom, percent] = Decode<Commands::ChangeServiceInterval>(data);
			parameters["vehicle_id"] = NativeVehicle(vehicle);
			parameters["mode"] = custom ? "custom" : "default";
			if (custom) { parameters["interval"] = interval; parameters["percent"] = percent; }
			action = "vehicle.service_interval.set";
			break;
		}
		case Commands::ForceTrainProceed: {
			auto [vehicle] = Decode<Commands::ForceTrainProceed>(data);
			parameters["vehicle_id"] = NativeVehicle(vehicle);
			action = "train.force_proceed";
			break;
		}
		case Commands::ReverseTrainDirection: {
			auto [vehicle, one] = Decode<Commands::ReverseTrainDirection>(data);
			if (!one) return std::nullopt;
			parameters["vehicle_id"] = NativeVehicle(vehicle);
			action = "train.unit.flip";
			break;
		}
		case Commands::AutoreplaceVehicle: {
			auto [vehicle] = Decode<Commands::AutoreplaceVehicle>(data);
			parameters["vehicle_id"] = NativeVehicle(vehicle);
			action = "vehicle.autoreplace";
			break;
		}
		case Commands::SetVehicleVisibility: {
			auto [engine, hidden] = Decode<Commands::SetVehicleVisibility>(data);
			parameters = {{"engine_id", engine.base()}, {"hidden", hidden}};
			action = "catalog.engine.visibility.set";
			break;
		}
		case Commands::ChangeCompanySetting: {
			auto [name, value] = Decode<Commands::ChangeCompanySetting>(data);
			const SettingKey *key = FindSettingKeyAlias(name);
			if (key == nullptr || !MatchesNativeDescriptor(*key, GetSettingFromName(key->native_name))) {
				throw InvalidParameters("Native company setting is outside the allowlist");
			}
			if (RendererIsActive() && !RendererCompanySettingCommandMatches(name)) {
				throw InvalidParameters("Visual company setting has no matching native control");
			}
			if (key->boolean && value != 0 && value != 1) throw InvalidParameters("Native boolean company setting is invalid");
			parameters = {{"key", key->public_name}, {"value", key->boolean ? Json(value == 1) : Json(value)}};
			if (SettingValue(parameters, *key) != value) throw InvalidParameters("Native company setting value is invalid");
			action = "company.setting.set";
			break;
		}
		default: return std::nullopt;
	}
	auto proof = BuildManagementTargets(action, parameters, false);
	if (!proof.has_value()) throw InvalidParameters("Native management proof is unavailable");
	return *proof;
}

} // namespace Integration::Detail
