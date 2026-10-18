/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file test_newgrf_industry_observation_overlay.cpp Query-local NewGRF PSA isolation. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../newgrf_industries.h"
#include "../core/random_func.hpp"
#include "../industry.h"
#include "../industry_presentation.h"
#include "../industrytype.h"
#include "../cargotype.h"
#include "../newgrf_spritegroup.h"
#include "../newgrf_config.h"
#include "../window_func.h"
#include "../openttd.h"
#include "../core/backup_type.hpp"
#include "../settings_type.h"
#include "../strings_func.h"
#include "../language.h"
#include "../core/endian_func.hpp"
#include <filesystem>
#include <fstream>
#include <chrono>
#include "table/strings.h"
#include "../map_func.h"
#include "../newgrf.h"
#include "../newgrf_storage.h"
#include "../newgrf_town.h"
#include "../town.h"
#include "../clear_map.h"
#include "../house.h"
#include "../industry_map.h"
#include "../newgrf_callbacks.h"
#include "../station_func.h"
#include "../town_map.h"

#include "../safeguards.h"

namespace {

struct OverlayWorld {
	TileIndex industry_tile;
	Town *town;
	Industry *industry;
	GRFFile active_file;
	GRFFile other_file;

	explicit OverlayWorld(uint map_size = 64)
	{
		_persistent_storage_pool.CleanPool();
		_industry_pool.CleanPool();
		_town_pool.CleanPool();
		Map::Allocate(map_size, map_size);
		industry_tile = TileXY(12, 12);
		REQUIRE(Town::CanAllocateItem());
		REQUIRE(Industry::CanAllocateItem());
		town = Town::Create(TileXY(10, 10));
		industry = Industry::Create(industry_tile);
		industry->town = town;
		industry->type = 0;
		active_file.grfid = GrfID{"\x01\x02\x03\x04"};
		other_file.grfid = GrfID{"\x05\x06\x07\x08"};
	}

	~OverlayWorld()
	{
		_persistent_storage_pool.CleanPool();
		_industry_pool.CleanPool();
		_town_pool.CleanPool();
	}

	IndustryPresentationPsaOverlay Overlay() const
	{
		return {industry, town, active_file.grfid};
	}

	PersistentStorage *Storage(GrfID grfid)
	{
		REQUIRE(PersistentStorage::CanAllocateItem());
		return PersistentStorage::Create(grfid, GrfSpecFeature::Industries, industry_tile);
	}
};

void ExpectRandomUnchanged(const Randomizer &game, const Randomizer &interactive)
{
	CHECK(_random.state[0] == game.state[0]);
	CHECK(_random.state[1] == game.state[1]);
	CHECK(_interactive_random.state[0] == interactive.state[0]);
	CHECK(_interactive_random.state[1] == interactive.state[1]);
}

} // namespace

TEST_CASE("Industry observation PSA overlay handles absent storage and cross-callback reads", "[integration][industry-psa]")
{
	OverlayWorld world;
	const auto game_random = _random, interactive_random = _interactive_random;
	const auto psa_generation = PersistentStorage::GetLastInstanceGeneration();
	const auto industry_generation = Industry::GetLastInstanceGeneration();
	const auto town_generation = Town::GetLastInstanceGeneration();
	const auto psa_count = PersistentStorage::GetNumItems();
	auto overlay = world.Overlay();
	ResolverObject first(&world.active_file);
	first.SetRegister(0x100, FlattenNewGRFLabel(INVALID_GRFID));
	IndustriesScopeResolver first_industry(first, world.industry_tile, world.industry, world.industry->type, 0, &overlay);
	TownScopeResolver first_town(first, world.town, false, &overlay);
	bool available = true;
	CHECK(first_industry.GetVariable(0x7C, 5, available) == 0);
	CHECK(first_town.GetVariable(0x7C, 9, available) == 0);
	first_industry.StorePSA(5, 17);
	first_town.StorePSA(9, 23);
	CHECK(first_industry.GetVariable(0x7C, 5, available) == 17);
	CHECK(first_town.GetVariable(0x7C, 9, available) == 23);

	/* A later GUI callback gets a new resolver but the same presentation overlay. */
	ResolverObject later(&world.active_file);
	later.SetRegister(0x100, FlattenNewGRFLabel(INVALID_GRFID));
	IndustriesScopeResolver later_industry(later, world.industry_tile, world.industry, world.industry->type, 0, &overlay);
	TownScopeResolver later_town(later, world.town, false, &overlay);
	CHECK(later_industry.GetVariable(0x7C, 5, available) == 17);
	CHECK(later_town.GetVariable(0x7C, 9, available) == 23);
	later_industry.StorePSA(256, 99);
	later_town.StorePSA(256, 99);
	CHECK(later_industry.GetVariable(0x7C, 256, available) == 0);
	CHECK(later_town.GetVariable(0x7C, 256, available) == 0);
	CHECK(world.industry->psa == nullptr);
	CHECK(world.town->psa_list.empty());
	CHECK(PersistentStorage::GetNumItems() == psa_count);
	CHECK(PersistentStorage::GetLastInstanceGeneration() == psa_generation);
	CHECK(Industry::GetLastInstanceGeneration() == industry_generation);
	CHECK(Town::GetLastInstanceGeneration() == town_generation);
	ExpectRandomUnchanged(game_random, interactive_random);

	/* A second request must begin from live PSA, not yesterday's query shadow. */
	auto next_request = world.Overlay();
	IndustriesScopeResolver fresh_industry(later, world.industry_tile, world.industry, world.industry->type, 0, &next_request);
	TownScopeResolver fresh_town(later, world.town, false, &next_request);
	CHECK(fresh_industry.GetVariable(0x7C, 5, available) == 0);
	CHECK(fresh_town.GetVariable(0x7C, 9, available) == 0);
}

TEST_CASE("Industry observation PSA overlay uses existing storage as fallback and preserves GRFID isolation", "[integration][industry-psa]")
{
	OverlayWorld world;
	PersistentStorage *industry_psa = world.Storage(world.active_file.grfid);
	industry_psa->storage[5] = 41;
	world.industry->psa = industry_psa;
	PersistentStorage *town_psa = world.Storage(world.active_file.grfid);
	town_psa->storage[9] = 43;
	world.town->psa_list.push_back(town_psa);
	PersistentStorage *other_psa = world.Storage(world.other_file.grfid);
	other_psa->storage[9] = 47;
	world.town->psa_list.push_back(other_psa);
	const auto game_random = _random, interactive_random = _interactive_random;
	const auto psa_generation = PersistentStorage::GetLastInstanceGeneration();
	const auto count = PersistentStorage::GetNumItems();
	const auto list_size = world.town->psa_list.size();
	auto overlay = world.Overlay();
	ResolverObject ro(&world.active_file);
	ro.SetRegister(0x100, FlattenNewGRFLabel(INVALID_GRFID));
	IndustriesScopeResolver industry_scope(ro, world.industry_tile, world.industry, world.industry->type, 0, &overlay);
	TownScopeResolver town_scope(ro, world.town, false, &overlay);
	bool available = true;
	CHECK(industry_scope.GetVariable(0x7C, 5, available) == 41);
	CHECK(town_scope.GetVariable(0x7C, 9, available) == 43);
	industry_scope.StorePSA(5, 71);
	town_scope.StorePSA(9, 73);
	CHECK(industry_scope.GetVariable(0x7C, 5, available) == 71);
	CHECK(town_scope.GetVariable(0x7C, 9, available) == 73);

	/* Other-GRF reads remain live; other-GRF Stop writes remain forbidden. */
	ro.SetRegister(0x100, FlattenNewGRFLabel(world.other_file.grfid));
	CHECK(town_scope.GetVariable(0x7C, 9, available) == 47);
	town_scope.StorePSA(9, 91);
	CHECK(town_scope.GetVariable(0x7C, 9, available) == 47);
	ro.SetRegister(0x100, FlattenNewGRFLabel(INVALID_GRFID));
	CHECK(town_scope.GetVariable(0x7C, 9, available) == 73);
	CHECK(industry_psa->storage[5] == 41);
	CHECK(town_psa->storage[9] == 43);
	CHECK(other_psa->storage[9] == 47);
	CHECK(world.industry->psa == industry_psa);
	CHECK(world.town->psa_list.size() == list_size);
	CHECK(PersistentStorage::GetNumItems() == count);
	CHECK(PersistentStorage::GetLastInstanceGeneration() == psa_generation);
	ExpectRandomUnchanged(game_random, interactive_random);

	/* Stack unwinding discards shadow writes without touching live PSA. */
	try {
		auto doomed = world.Overlay();
		IndustriesScopeResolver doomed_scope(ro, world.industry_tile, world.industry, world.industry->type, 0, &doomed);
		doomed_scope.StorePSA(5, 101);
		throw std::runtime_error("abort presentation");
	} catch (const std::runtime_error &) { }
	CHECK(industry_psa->storage[5] == 41);
	CHECK(PersistentStorage::GetNumItems() == count);
	CHECK(PersistentStorage::GetLastInstanceGeneration() == psa_generation);
	ExpectRandomUnchanged(game_random, interactive_random);
}

TEST_CASE("Ordinary industry and town scope writes still use live PSA without an overlay", "[integration][industry-psa]")
{
	OverlayWorld world;
	PersistentStorage *industry_psa = world.Storage(world.active_file.grfid);
	industry_psa->storage[5] = 3;
	world.industry->psa = industry_psa;
	PersistentStorage *town_psa = world.Storage(world.active_file.grfid);
	town_psa->storage[9] = 4;
	world.town->psa_list.push_back(town_psa);
	ResolverObject ro(&world.active_file);
	ro.SetRegister(0x100, FlattenNewGRFLabel(INVALID_GRFID));
	IndustriesScopeResolver normal_industry(ro, world.industry_tile, world.industry, world.industry->type);
	TownScopeResolver normal_town(ro, world.town, false);
	normal_industry.StorePSA(5, 8);
	normal_town.StorePSA(9, 12);
	CHECK(industry_psa->GetValue(5) == 8);
	CHECK(town_psa->GetValue(9) == 12);
	bool available = true;
	CHECK(normal_industry.GetVariable(0x7C, 5, available) == 8);
	CHECK(normal_town.GetVariable(0x7C, 9, available) == 12);
}

TEST_CASE("Industry presentation replays actual callback 37 writes and renders GUI lines", "[integration][industry-presentation]")
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
	const auto game_random = _random, interactive_random = _interactive_random;
	GRFFile grf;
	struct FixtureCleanup {
		IndustrySpec previous_spec;
		CargoSpec previous_cargo;
		size_t previous_config_count;
		std::vector<std::unique_ptr<SpriteGroup>> groups;
		~FixtureCleanup()
		{
			/* Restore spec pointers before deleting fixture-owned groups or GRFFile. */
			_industry_specs[0] = std::move(this->previous_spec);
			this->groups.clear();
			_grfconfig.resize(this->previous_config_count);
			*CargoSpec::Get(0) = std::move(this->previous_cargo);
			_persistent_storage_pool.CleanPool();
			_industry_pool.CleanPool();
			_town_pool.CleanPool();
		}
	};
	FixtureCleanup cleanup{_industry_specs[0], *CargoSpec::Get(0), _grfconfig.size(), {}};
	_persistent_storage_pool.CleanPool();
	_industry_pool.CleanPool();
	_town_pool.CleanPool();
	Map::Allocate(64, 64);
	CargoType cargo = static_cast<CargoType>(0);
	auto *cargo_spec = CargoSpec::Get(cargo);
	cargo_spec->bitnum = 0;
	cargo_spec->label = CT_PASSENGERS;
	cargo_spec->name = STR_CARGO_PLURAL_PASSENGERS;
	cargo_spec->name_single = STR_CARGO_SINGULAR_PASSENGER;
	cargo_spec->quantifier = STR_QUANTITY_PASSENGERS;
	REQUIRE(cargo_spec->IsValid());
	REQUIRE(Town::CanAllocateItem());
	Town *town = Town::Create(TileXY(10, 10));
	REQUIRE(Industry::CanAllocateItem());
	Industry *industry = Industry::Create(TileXY(12, 12));
	industry->town = town;
	industry->type = 0;
	industry->text = GetEncodedString(STR_INDUSTRY_VIEW_REQUIRES);
	industry->accepted.resize(2);
	industry->accepted[0].cargo = cargo;
	industry->accepted[0].waiting = 113;
	industry->accepted[1].cargo = cargo;
	industry->accepted[1].waiting = 137;
	industry->produced.resize(1);
	industry->produced[0].cargo = cargo;
	industry->produced[0].history[LAST_MONTH].production = 59;
	industry->produced[0].history[LAST_MONTH].transported = 29;

	grf.grf_version = 8;
	grf.grfid = GrfID{"\x0A\x0B\x0C\x0D"};
	auto &spec = _industry_specs[0];
	spec.behaviour = {};
	spec.callback_mask = {};
	spec.grf_prop = {};
	spec.grf_prop.SetGRFFile(&grf);
	spec.callback_mask.Set(IndustryCallbackMask::CargoSuffix);
	spec.callback_mask.Set(IndustryCallbackMask::WindowMoreText);
	spec.callback_mask.Set(IndustryCallbackMask::ProductionCargoArrival);

	/* The first accepted callback writes 0x401 to industry PSA[5] but reports
	 * 0x400 itself. The next accepted callback reads PSA[5] and reports 0x401.
	 * The view line proves the query replay carried that shadow write forward. */
	REQUIRE(SpriteGroup::CanAllocateItem(4));
	auto *default_group = CallbackResultSpriteGroup::Create(0x400);
	cleanup.groups.emplace_back(default_group);
	auto *writer = DeterministicSpriteGroup::Create();
	cleanup.groups.emplace_back(writer);
	writer->var_scope = VarSpriteGroupScope::Self;
	writer->size = DeterministicSpriteGroupSize::DWord;
	DeterministicSpriteGroupAdjust constant;
	constant.variable = 0x18;
	constant.and_mask = 0;
	constant.type = DeterministicSpriteGroupAdjustType::Div;
	constant.divmod_val = 1;
	constant.add_val = 0x401;
	constant.operation = DeterministicSpriteGroupAdjustOperation::Add;
	writer->adjusts.push_back(constant);
	DeterministicSpriteGroupAdjust stop;
	stop.variable = 0x18;
	stop.and_mask = 5;
	stop.type = DeterministicSpriteGroupAdjustType::Div;
	stop.divmod_val = 1;
	stop.add_val = 5;
	stop.operation = DeterministicSpriteGroupAdjustOperation::Stop;
	writer->adjusts.push_back(stop);
	writer->default_result.group = default_group;

	auto *reader = DeterministicSpriteGroup::Create();
	cleanup.groups.emplace_back(reader);
	reader->var_scope = VarSpriteGroupScope::Self;
	reader->size = DeterministicSpriteGroupSize::DWord;
	DeterministicSpriteGroupAdjust read;
	read.variable = 0x7C;
	read.parameter = 5;
	read.and_mask = 0xFFFF;
	read.operation = DeterministicSpriteGroupAdjustOperation::Add;
	reader->adjusts.push_back(read);
	reader->default_result.calculated_result = true;

	auto *dispatch = DeterministicSpriteGroup::Create();
	cleanup.groups.emplace_back(dispatch);
	dispatch->var_scope = VarSpriteGroupScope::Self;
	dispatch->size = DeterministicSpriteGroupSize::DWord;
	DeterministicSpriteGroupAdjust selector;
	selector.variable = 0x18;
	selector.and_mask = 0xFFFF;
	selector.operation = DeterministicSpriteGroupAdjustOperation::Add;
	dispatch->adjusts.push_back(selector);
	dispatch->default_result.group = default_group;
	dispatch->ranges.push_back({{false, writer}, 0x100, 0x100});
	dispatch->ranges.push_back({{false, reader}, 0x101, 0x101});
	dispatch->ranges.push_back({{false, reader}, 0x103, 0x103});
	dispatch->ranges.push_back({{false, reader}, 0, 0});
	spec.grf_prop.SetSpriteGroup(StandardSpriteGroup::Default, dispatch);

	const auto psa_generation = PersistentStorage::GetLastInstanceGeneration();
	const auto psa_count = PersistentStorage::GetNumItems();
	IndustryPresentationPsaOverlay overlay(industry, town, grf.grfid);
	CHECK(GetIndustryCallbackForObservation(CBID_INDUSTRY_CARGO_SUFFIX, 0, 0x100, industry, 0, industry->location.tile, {}, overlay) == 0x400);
	CHECK(GetIndustryCallbackForObservation(CBID_INDUSTRY_CARGO_SUFFIX, 0, 0x101, industry, 0, industry->location.tile, {}, overlay) == 0x401);
	CHECK(overlay.industry_writes.at(industry)[5] == 0x401);
	CHECK(industry->psa == nullptr);

	auto first = RenderIndustryDisplayText(*industry, IndustryDisplayTextKind::AcceptedLine, 0);
	auto second = RenderIndustryDisplayText(*industry, IndustryDisplayTextKind::AcceptedLine, 1);
	auto produced = RenderIndustryDisplayText(*industry, IndustryDisplayTextKind::ProducedLine, 0);
	auto extra = RenderIndustryDisplayText(*industry, IndustryDisplayTextKind::ExtraText);
	auto name = RenderIndustryDisplayText(*industry, IndustryDisplayTextKind::Name);
	auto custom = RenderIndustryDisplayText(*industry, IndustryDisplayTextKind::CustomText);
	CHECK(first == GetString(STR_INDUSTRY_VIEW_ACCEPT_CARGO_AMOUNT_NOSUFFIX, CargoSpec::Get(cargo)->name, cargo, 113, std::string{}));
	CHECK(second == GetString(STR_INDUSTRY_VIEW_ACCEPT_CARGO_NOSUFFIX, CargoSpec::Get(cargo)->name, cargo, 137, std::string{}));
	CHECK(produced == GetString(STR_INDUSTRY_VIEW_TRANSPORTED, cargo, 59, std::string{}, ToPercent8(industry->produced[0].history[LAST_MONTH].PctTransported())));
	CHECK(extra.empty());
	CHECK(name == GetString(STR_INDUSTRY_NAME, industry->index));
	CHECK(custom == GetString(STR_INDUSTRY_VIEW_REQUIRES));
	CHECK(industry->psa == nullptr);
	CHECK(town->psa_list.empty());
	CHECK(PersistentStorage::GetNumItems() == psa_count);
	CHECK(PersistentStorage::GetLastInstanceGeneration() == psa_generation);
	ExpectRandomUnchanged(game_random, interactive_random);

	/* Unknown callback values in both GRF versions must not report a GUI error
	 * or mutate the GRF bug latch while an observation renders. */
	auto config = std::make_unique<GRFConfig>();
	config->ident.grfid = grf.grfid;
	config->grf_bugs.Set(GRFBug::UnknownCbResult);
	GRFConfig *config_ptr = config.get();
	_grfconfig.push_back(std::move(config));
	Window *error_window = FindWindowById(WindowClass::ErrorMessage, 0);
	REQUIRE(error_window == nullptr);
	AutoRestoreBackup mode_backup(_game_mode, GameMode::Normal);
	REQUIRE(SpriteGroup::CanAllocateItem());
	auto *unknown = CallbackResultSpriteGroup::Create(0xC00);
	cleanup.groups.emplace_back(unknown);
	spec.grf_prop.SetSpriteGroup(StandardSpriteGroup::Default, unknown);
	grf.grf_version = 8;
	CHECK(!RenderIndustryDisplayText(*industry, IndustryDisplayTextKind::AcceptedLine, 0).empty());
	CHECK(RenderIndustryDisplayText(*industry, IndustryDisplayTextKind::ExtraText).empty());
	grf.grf_version = 7;
	CHECK(!RenderIndustryDisplayText(*industry, IndustryDisplayTextKind::AcceptedLine, 0).empty());
	CHECK(FindWindowById(WindowClass::ErrorMessage, 0) == error_window);
	CHECK(config_ptr->grf_bugs.Test(GRFBug::UnknownCbResult));
	CHECK(industry->psa == nullptr);
	CHECK(PersistentStorage::GetNumItems() == psa_count);
	ExpectRandomUnchanged(game_random, interactive_random);
}

namespace {

/** Genuine callback chains shared by the industry-tile and house cargo tests. */
struct CargoOverlayWorld : OverlayWorld {
	IndustrySpec previous_industry = _industry_specs[0];
	std::array<IndustryTileSpec, 2> previous_tiles{_industry_tile_specs[0], _industry_tile_specs[1]};
	std::vector<HouseSpec> previous_houses = HouseSpec::Specs();
	CargoSpec previous_cargo = *CargoSpec::Get(0);
	std::vector<std::unique_ptr<SpriteGroup>> groups;

	CargoOverlayWorld() : OverlayWorld(128)
	{
		for (uint y = 0; y < Map::SizeY(); ++y) {
			for (uint x = 0; x < Map::SizeX(); ++x) {
				const TileIndex tile = TileXY(x, y);
				if (IsInnerTile(tile)) MakeClear(tile, ClearGround::Grass, 3);
			}
		}
		active_file.grf_version = other_file.grf_version = 8;
		active_file.cargo_list = other_file.cargo_list = {CT_PASSENGERS};
		CargoSpec::Get(0)->bitnum = 0;
		CargoSpec::Get(0)->label = CT_PASSENGERS;
		BuildCargoLabelMap();
		_industry_specs[0].grf_prop.SetGRFFile(&active_file);
		HouseSpec::Specs().resize(std::max<size_t>(2, HouseSpec::Specs().size()));
	}

	~CargoOverlayWorld()
	{
		_industry_specs[0] = std::move(previous_industry);
		_industry_tile_specs[0] = std::move(previous_tiles[0]);
		_industry_tile_specs[1] = std::move(previous_tiles[1]);
		HouseSpec::Specs() = std::move(previous_houses);
		*CargoSpec::Get(0) = std::move(previous_cargo);
		BuildCargoLabelMap();
		groups.clear();
	}

	DeterministicSpriteGroup *Group(VarSpriteGroupScope scope)
	{
		REQUIRE(SpriteGroup::CanAllocateItem());
		auto *group = DeterministicSpriteGroup::Create();
		groups.emplace_back(group);
		group->var_scope = scope;
		group->size = DeterministicSpriteGroupSize::DWord;
		return group;
	}

	const SpriteGroup *Result(uint16_t value)
	{
		REQUIRE(SpriteGroup::CanAllocateItem());
		auto *group = CallbackResultSpriteGroup::Create(value);
		groups.emplace_back(group);
		return group;
	}

	static DeterministicSpriteGroupAdjust Variable(uint8_t variable, uint32_t parameter = 0)
	{
		DeterministicSpriteGroupAdjust adjust;
		adjust.variable = variable;
		adjust.parameter = parameter;
		adjust.and_mask = UINT32_MAX;
		adjust.operation = DeterministicSpriteGroupAdjustOperation::Rst;
		return adjust;
	}

	static DeterministicSpriteGroupAdjust Constant(uint32_t value, DeterministicSpriteGroupAdjustOperation operation = DeterministicSpriteGroupAdjustOperation::Rst)
	{
		auto adjust = Variable(0x18);
		adjust.and_mask = 0;
		adjust.type = DeterministicSpriteGroupAdjustType::Div;
		adjust.divmod_val = 1;
		adjust.add_val = value;
		adjust.operation = operation;
		return adjust;
	}

	static void SelectTownGRF(DeterministicSpriteGroup *group, GrfID grfid)
	{
		group->adjusts.push_back(Constant(FlattenNewGRFLabel(grfid)));
		group->adjusts.push_back(Constant(0x100, DeterministicSpriteGroupAdjustOperation::Sto));
	}

	DeterministicSpriteGroup *Increment(uint slot, uint increment, bool town_scope)
	{
		auto *group = Group(VarSpriteGroupScope::Parent);
		if (town_scope) SelectTownGRF(group, INVALID_GRFID);
		group->adjusts.push_back(Variable(0x7C, slot));
		group->adjusts.push_back(Constant(increment, DeterministicSpriteGroupAdjustOperation::Add));
		group->adjusts.push_back(Constant(slot, DeterministicSpriteGroupAdjustOperation::Stop));
		group->default_result.calculated_result = true;
		return group;
	}

	void IndustryTile(IndustryGfx gfx)
	{
		auto &spec = _industry_tile_specs[gfx];
		spec = IndustryTileSpec{};
		spec.grf_prop.SetGRFFile(&active_file);
		spec.callback_mask.Set(IndustryTileCallbackMask::AcceptCargo);
		spec.callback_mask.Set(IndustryTileCallbackMask::CargoAcceptance);
		auto *writer = Increment(5, 1, false);
		writer->default_result = {false, Result(0)};
		auto *reader = Group(VarSpriteGroupScope::Parent);
		reader->adjusts.push_back(Variable(0x7C, 5));
		reader->default_result.calculated_result = true;
		auto *dispatch = Group(VarSpriteGroupScope::Self);
		dispatch->adjusts.push_back(Variable(0x0C));
		dispatch->default_result.group = Result(CALLBACK_FAILED);
		dispatch->ranges.push_back({{false, reader}, CBID_INDTILE_CARGO_ACCEPTANCE, CBID_INDTILE_CARGO_ACCEPTANCE});
		dispatch->ranges.push_back({{false, writer}, CBID_INDTILE_ACCEPT_CARGO, CBID_INDTILE_ACCEPT_CARGO});
		spec.grf_prop.SetSpriteGroup(StandardSpriteGroup::Default, dispatch);
	}

	/** Produce one cargo entry iff the selected town PSA has been written. */
	const SpriteGroup *Production(GrfID read_grfid)
	{
		auto *read = Group(VarSpriteGroupScope::Parent);
		SelectTownGRF(read, read_grfid);
		read->adjusts.push_back(Variable(0x7C, 9));
		read->default_result.group = Result(0);
		read->ranges.push_back({{false, Result(CALLBACK_HOUSEPRODCARGO_END)}, 0, 0});
		auto *iteration = Group(VarSpriteGroupScope::Self);
		iteration->adjusts.push_back(Variable(0x10));
		iteration->default_result.group = Result(CALLBACK_HOUSEPRODCARGO_END);
		iteration->ranges.push_back({{false, read}, 0, 0});
		return iteration;
	}

	void House(HouseID id, GRFFile &file, const SpriteGroup *acceptance, const SpriteGroup *production)
	{
		auto &spec = *HouseSpec::Get(id);
		spec = HouseSpec{};
		std::fill(std::begin(spec.accepts_cargo), std::end(spec.accepts_cargo), INVALID_CARGO);
		spec.accepts_cargo[0] = static_cast<CargoType>(0);
		spec.callback_mask.Set(HouseCallbackMask::CargoAcceptance);
		spec.callback_mask.Set(HouseCallbackMask::ProduceCargo);
		spec.grf_prop.SetGRFFile(&file);
		spec.grf_prop.override_id = INVALID_HOUSE_ID;
		auto *dispatch = Group(VarSpriteGroupScope::Self);
		dispatch->adjusts.push_back(Variable(0x0C));
		dispatch->default_result.group = Result(CALLBACK_FAILED);
		dispatch->ranges.push_back({{false, acceptance}, CBID_HOUSE_CARGO_ACCEPTANCE, CBID_HOUSE_CARGO_ACCEPTANCE});
		dispatch->ranges.push_back({{false, production}, CBID_HOUSE_PRODUCE_CARGO, CBID_HOUSE_PRODUCE_CARGO});
		spec.grf_prop.SetSpriteGroup(StandardSpriteGroup::Default, dispatch);
	}
};

} // namespace

TEST_CASE("Station cargo observation carries actual industry-tile PSA callbacks across owners and tiles", "[integration][industry-psa][station-site]")
{
	CargoOverlayWorld world;
	REQUIRE(Industry::CanAllocateItem());
	Industry *second = Industry::Create(TileXY(13, 12));
	second->town = world.town;
	second->type = 0;
	MakeIndustry(world.industry_tile, world.industry->index, 0, 37, WaterClass::Invalid);
	MakeIndustry(second->location.tile, second->index, 1, 53, WaterClass::Invalid);
	world.IndustryTile(0);
	world.IndustryTile(1);
	bool existing = false;
	SECTION("without native PSA") { }
	SECTION("with native PSA") { existing = true; }
	PersistentStorage *first_psa = nullptr, *second_psa = nullptr;
	if (existing) {
		world.industry->psa = first_psa = world.Storage(world.active_file.grfid);
		second->psa = second_psa = world.Storage(world.active_file.grfid);
		first_psa->storage[5] = 3;
		second_psa->storage[5] = 6;
	}
	const auto count = PersistentStorage::GetNumItems();
	const auto generation = PersistentStorage::GetLastInstanceGeneration();
	const auto game_random = _random, interactive_random = _interactive_random;
	auto overlay = world.Overlay();
	CHECK(GetAcceptanceAroundTiles(world.industry_tile, 2, 1, 0, &overlay).first[0] == (existing ? 11 : 2));
	CHECK(GetAcceptanceAroundTiles(world.industry_tile, 2, 1, 0, &overlay).first[0] == (existing ? 13 : 4));
	CHECK(overlay.industry_writes.at(world.industry)[5] == (existing ? 5 : 2));
	CHECK(overlay.industry_writes.at(second)[5] == (existing ? 8 : 2));
	auto fresh = world.Overlay();
	CHECK(GetAcceptanceAroundTiles(world.industry_tile, 2, 1, 0, &fresh).first[0] == (existing ? 11 : 2));
	CHECK(world.industry->psa == first_psa);
	CHECK(second->psa == second_psa);
	if (existing) {
		CHECK(first_psa->storage[5] == 3);
		CHECK(second_psa->storage[5] == 6);
	}
	CHECK(world.town->psa_list.empty());
	CHECK(PersistentStorage::GetNumItems() == count);
	CHECK(PersistentStorage::GetLastInstanceGeneration() == generation);
	ExpectRandomUnchanged(game_random, interactive_random);
}

TEST_CASE("Station cargo observation shares actual house callbacks across passes towns and GRFs", "[integration][industry-psa][station-site]")
{
	CargoOverlayWorld world;
	REQUIRE(Town::CanAllocateItem());
	Town *second_town = Town::Create(TileXY(30, 30));
	auto *ordinary_acceptance = world.Increment(9, 1, true);
	world.House(0, world.active_file, ordinary_acceptance, world.Production(world.other_file.grfid));
	auto *other_acceptance = world.Increment(9, 3, true);
	/* A permitted own-GRF write precedes a read of another GRF's earlier write.
	 * Attempting to overwrite that other GRF must remain forbidden. */
	CargoOverlayWorld::SelectTownGRF(other_acceptance, world.active_file.grfid);
	other_acceptance->adjusts.push_back(CargoOverlayWorld::Constant(91));
	other_acceptance->adjusts.push_back(CargoOverlayWorld::Constant(9, DeterministicSpriteGroupAdjustOperation::Stop));
	other_acceptance->adjusts.push_back(CargoOverlayWorld::Variable(0x7C, 9));
	world.House(1, world.other_file, other_acceptance, world.Production(INVALID_GRFID));
	TileIndex start = TileXY(20, 20);
	MakeHouseTile(start, world.town->index, 0, TOWN_HOUSE_COMPLETED, 0, 17, false);
	MakeHouseTile(start + 1, world.town->index, 0, TOWN_HOUSE_COMPLETED, 1, 19, false);
	MakeHouseTile(start + 2, world.town->index, 0, TOWN_HOUSE_COMPLETED, 0, 23, false);
	MakeHouseTile(start + 3, second_town->index, 0, TOWN_HOUSE_COMPLETED, 0, 29, false);
	world.IndustryTile(0);
	world.industry->location = TileArea(start + 4, 1, 1);
	world.industry->produced.clear();
	MakeIndustry(start + 4, world.industry->index, 0, 31, WaterClass::Invalid);
	bool existing = false;
	SECTION("without native PSA") { }
	SECTION("with native PSA") { existing = true; }
	PersistentStorage *first_psa = nullptr, *second_psa = nullptr, *other_psa = nullptr;
	if (existing) {
		first_psa = world.Storage(world.active_file.grfid);
		second_psa = world.Storage(world.active_file.grfid);
		other_psa = world.Storage(world.other_file.grfid);
		first_psa->storage[9] = 3;
		second_psa->storage[9] = 6;
		other_psa->storage[9] = 11;
		world.town->psa_list = {first_psa, other_psa};
		second_town->psa_list = {second_psa};
	}
	const auto count = PersistentStorage::GetNumItems();
	const auto generation = PersistentStorage::GetLastInstanceGeneration();
	const auto game_random = _random, interactive_random = _interactive_random;
	auto overlay = world.Overlay();
	CHECK(GetAcceptanceAroundTiles(start, 5, 1, 0, &overlay).first[0] == (existing ? 21 : 6));
	/* Production must observe the preceding acceptance pass, even when the
	 * producer reads a different GRF's storage and uses a fresh resolver. */
	CHECK(GetProductionAroundTiles(start, 5, 1, 0, &overlay)[0] == 3);
	CHECK(overlay.industry_writes.at(world.industry)[5] == 1);
	CHECK(overlay.town_writes.at({world.town, world.active_file.grfid})[9] == (existing ? 5 : 2));
	CHECK(overlay.town_writes.at({world.town, world.other_file.grfid})[9] == (existing ? 14 : 3));
	CHECK(overlay.town_writes.at({second_town, world.active_file.grfid})[9] == (existing ? 7 : 1));
	CHECK(overlay.town_writes.size() == 3);
	auto fresh = world.Overlay();
	CHECK(GetProductionAroundTiles(start, 5, 1, 0, &fresh)[0] == (existing ? 3 : 0));
	CHECK(GetAcceptanceAroundTiles(start, 5, 1, 0, &fresh).first[0] == (existing ? 21 : 6));
	CHECK(world.town->psa_list == (existing ? std::vector<PersistentStorage *>{first_psa, other_psa} : std::vector<PersistentStorage *>{}));
	CHECK(second_town->psa_list == (existing ? std::vector<PersistentStorage *>{second_psa} : std::vector<PersistentStorage *>{}));
	if (existing) {
		CHECK(first_psa->storage[9] == 3);
		CHECK(second_psa->storage[9] == 6);
		CHECK(other_psa->storage[9] == 11);
	}
	CHECK(world.industry->psa == nullptr);
	CHECK(PersistentStorage::GetNumItems() == count);
	CHECK(PersistentStorage::GetLastInstanceGeneration() == generation);
	ExpectRandomUnchanged(game_random, interactive_random);
}

TEST_CASE("Station cargo observation runs the native256 probe house loop across a max rectangle", "[integration][industry-psa][station-site]")
{
	CargoOverlayWorld world;
	auto *production = world.Increment(9, 1, true);
	production->default_result = {false, world.Result(0)};
	world.House(0, world.active_file, world.Result(0), production);
	for (uint y = 1; y <= 64; ++y) {
		for (uint x = 1; x <= 64; ++x) {
			MakeHouseTile(TileXY(x, y), world.town->index, 0, TOWN_HOUSE_COMPLETED, 0, 0, false);
		}
	}
	const auto count = PersistentStorage::GetNumItems();
	const auto generation = PersistentStorage::GetLastInstanceGeneration();
	const auto game_random = _random, interactive_random = _interactive_random;
	auto overlay = world.Overlay();
	const auto started = std::chrono::steady_clock::now();
	const auto produced = GetProductionAroundTiles(TileXY(1, 1), 64, 64, 0, &overlay);
	const auto elapsed = std::chrono::steady_clock::now() - started;
	INFO("64x64 house callback production microseconds=" << std::chrono::duration_cast<std::chrono::microseconds>(elapsed).count());
	CHECK(produced[0] == 64 * 64 * 256);
	CHECK(overlay.town_writes.at({world.town, world.active_file.grfid})[9] == 64 * 64 * 256);
	CHECK(world.town->psa_list.empty());
	CHECK(PersistentStorage::GetNumItems() == count);
	CHECK(PersistentStorage::GetLastInstanceGeneration() == generation);
	ExpectRandomUnchanged(game_random, interactive_random);
}
