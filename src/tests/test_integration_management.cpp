/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License, version 2.
 */

/** @file test_integration_management.cpp Management proof and handle boundaries. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions_internal.h"
#include "../autoreplace_base.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../core/pool_func.hpp"
#include "../group.h"
#include "../group_cmd.h"
#include "../misc/endian_buffer.hpp"
#include "../vehicle_base.h"

#include "../safeguards.h"

namespace {

using Integration::Json;
using namespace Integration::Detail;

struct ManagementWorld {
	CompanyID previous_company = _current_company;
	Company *company;

	ManagementWorld()
	{
		_enginerenew_pool.CleanPool();
		_vehicle_pool.CleanPool();
		_group_pool.CleanPool();
		_company_pool.CleanPool();
		Map::Allocate(64, 64);
		REQUIRE(Company::CanAllocateItem());
		this->company = Company::Create();
		_current_company = this->company->index;
	}

	~ManagementWorld()
	{
		_enginerenew_pool.CleanPool();
		_vehicle_pool.CleanPool();
		_group_pool.CleanPool();
		_company_pool.CleanPool();
		_current_company = this->previous_company;
	}

	Group *NewGroup(CompanyID owner) const
	{
		REQUIRE(Group::CanAllocateItem());
		return Group::Create(owner, VehicleType::Train);
	}
};

template <Commands Tcommand, typename ... T>
CommandDataBuffer NativeData(T &&... values)
{
	using Args = typename CommandTraits<Tcommand>::Args;
	return EndianBufferWriter<CommandDataBuffer>::FromValue(Args{std::forward<T>(values)...});
}

} // namespace

TEST_CASE("Management native proof ignores replica-local group generations while typed handles fence them", "[integration][management]")
{
	ManagementWorld world;
	Group *group = world.NewGroup(world.company->index);
	const GroupID id = group->index;
	const uint64_t old_generation = group->GetInstanceGeneration();
	const Json old_handle = Handle(group);
	const Json rename = {{"group_id", old_handle}, {"name", "route"}};
	const Json removal = {{"group_id", old_handle}};
	const auto rename_data = NativeData<Commands::AlterGroup>(AlterGroupMode::Rename, id, GroupID::Invalid(), std::string("route"));
	const auto delete_data = NativeData<Commands::DeleteGroup>(id);
	const auto rename_native = ManagementIntentState(Commands::AlterGroup, rename_data);
	const auto delete_native = ManagementIntentState(Commands::DeleteGroup, delete_data);
	const auto rename_typed = CaptureManagementTargets("group.rename", rename);
	const auto delete_typed = CaptureManagementTargets("group.delete", removal);
	REQUIRE(rename_native.has_value());
	REQUIRE(delete_native.has_value());
	REQUIRE(rename_typed.has_value());
	REQUIRE(delete_typed.has_value());

	delete group;
	group = world.NewGroup(world.company->index);
	REQUIRE(group->index == id);
	REQUIRE(group->GetInstanceGeneration() != old_generation);
	CHECK(ManagementIntentState(Commands::AlterGroup, rename_data) == rename_native);
	CHECK(ManagementIntentState(Commands::DeleteGroup, delete_data) == delete_native);
	CHECK_THROWS_AS(CaptureManagementTargets("group.rename", rename), InvalidParameters);
	CHECK_THROWS_AS(CaptureManagementTargets("group.delete", removal), InvalidParameters);
	CHECK(CaptureManagementTargets("group.rename", {{"group_id", Handle(group)}, {"name", "route"}}) != rename_typed);
	CHECK(CaptureManagementTargets("group.delete", {{"group_id", Handle(group)}}) != delete_typed);
}

TEST_CASE("Management native proof follows livery and protection state without pool generation coupling", "[integration][management]")
{
	ManagementWorld world;
	Group *group = world.NewGroup(world.company->index);
	const auto data = NativeData<Commands::SetGroupLivery>(group->index, true, Colours::Blue);
	const auto before = ManagementIntentState(Commands::SetGroupLivery, data);
	REQUIRE(before.has_value());
	group->livery.colour1 = Colours::Red;
	CHECK(ManagementIntentState(Commands::SetGroupLivery, data) != before);
	group->livery.colour1 = Colours::DarkBlue;
	const auto base = ManagementIntentState(Commands::SetGroupLivery, data);
	group->flags.Set(GroupFlag::ReplaceProtection);
	CHECK(ManagementIntentState(Commands::SetGroupLivery, data) != base);

	/* A replacement rule is addressed by its logical group/old engine key.
	 * Recreating an identical rule at a different pool slot must not make a
	 * screenshot intent stale or change a typed action proof. */
	REQUIRE(EngineRenew::CanAllocateItem());
	auto *rule = EngineRenew::Create(EngineID(1), EngineID(2), group->index, false, nullptr);
	world.company->engine_renew_list = rule;
	const auto removal = NativeData<Commands::DeleteGroup>(group->index);
	const Json parameters = {{"group_id", Handle(group)}};
	const auto native_rule_proof = ManagementIntentState(Commands::DeleteGroup, removal);
	const auto typed_rule_proof = CaptureManagementTargets("group.delete", parameters);
	REQUIRE(native_rule_proof.has_value());
	REQUIRE(typed_rule_proof.has_value());
	const auto original_rule_id = rule->index;
	world.company->engine_renew_list = nullptr;
	delete rule;
	/* Occupy the former slot with an unrelated rule so the same logical rule
	 * receives a different pool ID as well as a different generation. */
	REQUIRE(EngineRenew::CanAllocateItem());
	auto *unrelated = EngineRenew::Create(EngineID(7), EngineID(8), ALL_GROUP, false, nullptr);
	REQUIRE(unrelated->index == original_rule_id);
	REQUIRE(EngineRenew::CanAllocateItem());
	rule = EngineRenew::Create(EngineID(1), EngineID(2), group->index, false, nullptr);
	REQUIRE(rule->index != original_rule_id);
	world.company->engine_renew_list = rule;
	CHECK(ManagementIntentState(Commands::DeleteGroup, removal) == native_rule_proof);
	CHECK(CaptureManagementTargets("group.delete", parameters) == typed_rule_proof);
	rule->to = EngineID(3);
	CHECK(ManagementIntentState(Commands::DeleteGroup, removal) != native_rule_proof);
	CHECK(CaptureManagementTargets("group.delete", parameters) != typed_rule_proof);
	rule->to = EngineID(2);
	rule->replace_when_old = true;
	CHECK(ManagementIntentState(Commands::DeleteGroup, removal) != native_rule_proof);
	CHECK(CaptureManagementTargets("group.delete", parameters) != typed_rule_proof);
	world.company->engine_renew_list = nullptr;
}

TEST_CASE("Management group scopes reject foreign groups and mixed selectors", "[integration][management]")
{
	ManagementWorld world;
	REQUIRE(Company::CanAllocateItem());
	Company *other = Company::Create();
	Group *foreign = world.NewGroup(other->index);
	CHECK_THROWS_AS(CaptureManagementTargets("group.rename", {{"group_id", Handle(foreign)}, {"name", "wrong owner"}}), InvalidParameters);
	const auto foreign_rename = NativeData<Commands::AlterGroup>(AlterGroupMode::Rename, foreign->index, GroupID::Invalid(), std::string("wrong owner"));
	CHECK_THROWS_AS(ManagementIntentState(Commands::AlterGroup, foreign_rename), InvalidParameters);
	Group *own = world.NewGroup(world.company->index);
	CHECK_THROWS_AS(CaptureManagementTargets("autoreplace.set", {{"scope", "all"}, {"group_id", Handle(own)}, {"old_engine_id", 0}, {"new_engine_id", 1}}), InvalidParameters);
}

TEST_CASE("Explicit control IDs select the current owned occupant without rebinding old actions", "[integration][control-reference]")
{
	ManagementWorld world;
	Group *group = world.NewGroup(world.company->index);
	const GroupID id = group->index;
	const Json old_context = Integration::ResolveControlReference({{"kind", "group"}, {"id", id.base()}});
	const Json old_action = {{"group_id", old_context.at("reference")}, {"name", "before"}};
	CHECK(CaptureManagementTargets("group.rename", old_action).has_value());
	delete group;
	group = world.NewGroup(world.company->index);
	REQUIRE(group->index == id);
	const Json fresh_context = Integration::ResolveControlReference({{"kind", "group"}, {"id", id.base()}});
	CHECK(fresh_context.size() == 2);
	CHECK(fresh_context.at("reference") != old_context.at("reference"));
	CHECK_THROWS_AS(CaptureManagementTargets("group.rename", old_action), InvalidParameters);
	CHECK(CaptureManagementTargets("group.rename", {{"group_id", fresh_context.at("reference")}, {"name", "after"}}).has_value());
	REQUIRE(Company::CanAllocateItem());
	group->owner = Company::Create()->index;
	CHECK_THROWS_AS(Integration::ResolveControlReference({{"kind", "group"}, {"id", id.base()}}), InvalidParameters);
}
