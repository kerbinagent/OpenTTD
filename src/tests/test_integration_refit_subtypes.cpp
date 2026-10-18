/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file test_integration_refit_subtypes.cpp Real native CargoSuffix observation bounds. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions_internal.h"
#include "../cargotype.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../core/endian_func.hpp"
#include "../core/pool_func.hpp"
#include "../core/random_func.hpp"
#include "../engine_base.h"
#include "../language.h"
#include "../newgrf.h"
#include "../newgrf_engine.h"
#include "../newgrf_spritegroup.h"
#include "../newgrf_text.h"
#include "../station_base.h"
#include "../strings_func.h"
#include "../train.h"
#include "../vehicle_base.h"

#include <filesystem>
#include <fstream>
#include <thread>

#include "table/strings.h"

#include "../safeguards.h"

namespace {

using Integration::Json;
using Integration::Detail::Handle;
using Integration::Detail::InvalidParameters;
using Integration::Detail::QueryVehicleDetailsState;
using Integration::Detail::QueryWorldDetailsState;
using Integration::Detail::ResolveVehicleRefitLabelString;

struct RefitWorld {
	CompanyID previous_company = _current_company;
	Randomizer previous_random = _random;
	Randomizer previous_interactive_random = _interactive_random;
	GRFFile grf{};
	Company *company = nullptr;
	Engine *engine = nullptr;
	Train *head = nullptr;
	Train *tail = nullptr;
	CargoType cargo = INVALID_CARGO;
	StringID label_id{};
	std::string full_label = std::string(127, 'a') + "\xC3\xA9" + " tail";
	std::vector<Train *> parts;

	RefitWorld()
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
		_engine_pool.CleanPool();
		_station_pool.CleanPool();
		_company_pool.CleanPool();
		_spritegroup_pool.CleanPool();
		Map::Allocate(64, 64);
		SetupCargoForClimate(LandscapeType::Temperate);
		this->cargo = GetCargoTypeByLabel(CT_PASSENGERS);
		REQUIRE(this->cargo != INVALID_CARGO);
		REQUIRE(Company::CanAllocateItem());
		this->company = Company::Create();
		_current_company = this->company->index;
		REQUIRE(Engine::CanAllocateItem());
		this->engine = Engine::Create(VehicleType::Train, 0);
		this->engine->info.cargo_type = this->cargo;
		this->engine->VehInfo<RailVehicleInfo>().capacity = 40;
		this->engine->info.refit_mask.Set(this->cargo);
		this->engine->info.callback_mask.Set(VehicleCallbackMask::CargoSuffix);
		this->grf.grf_version = 8;
		this->grf.grfid = GrfID{"\x01\x02\x03\x04"};
		SetEngineGRF(this->engine->index, &this->grf);
		this->label_id = AddGRFString(this->grf.grfid, GRFSTR_MISC_GRF_TEXT, static_cast<uint8_t>(GRFLanguage::English), true, true,
			this->full_label, STR_UNDEFINED);
		this->SetCallbackResult(0); // Never returns STR_EMPTY: all 255 numeric values are probed.
		REQUIRE(Train::CanAllocateItem());
		this->head = Train::Create();
		this->head->owner = this->company->index;
		this->head->engine_type = this->engine->index;
		this->head->tile = TileXY(1, 1);
		this->head->subtype |= 1 << GVSF_FRONT;
		this->head->cargo_type = this->cargo;
		this->head->cargo_cap = 40;
		this->parts.push_back(this->head);
		this->tail = this->head;
		_random.SetSeed(12345);
	}

	~RefitWorld()
	{
		_vehicle_pool.CleanPool();
		_engine_pool.CleanPool();
		_station_pool.CleanPool();
		_company_pool.CleanPool();
		_spritegroup_pool.CleanPool();
		CleanUpStrings();
		_current_company = this->previous_company;
		_random = this->previous_random;
		_interactive_random = this->previous_interactive_random;
	}

	void SetCallbackResult(uint16_t callback)
	{
		REQUIRE(CallbackResultSpriteGroup::CanAllocateItem());
		this->engine->grf_prop.SetSpriteGroup(CargoGRFFileProps::SG_DEFAULT, CallbackResultSpriteGroup::Create(callback));
	}

	void AddPart()
	{
		REQUIRE(Train::CanAllocateItem());
		Train *part = Train::Create();
		part->owner = this->company->index;
		part->engine_type = this->engine->index;
		part->tile = this->head->tile;
		part->cargo_type = this->cargo;
		part->cargo_cap = 40;
		this->tail->SetNext(part);
		this->tail = part;
		this->parts.push_back(part);
	}

	Json Parameters() const
	{
		return {{"vehicle_id", Handle(this->head)}, {"part_id", Handle(this->tail)},
			{"cargo_id", to_underlying(this->cargo)}, {"after_subtype", -1}, {"limit", 16}};
	}
};

TEST_CASE("vehicle refit subtypes uses real callback without gameplay writes", "[integration_refit_subtypes]")
{
	/* This gate is shared with world text observations that may run earlier in
	 * the same Catch process. */
	std::this_thread::sleep_for(std::chrono::milliseconds(1100));
	RefitWorld world;
	for (int i = 1; i < 1024; ++i) world.AddPart();
	REQUIRE(world.parts.size() == 1024);
	world.tail->cargo_subtype = 17;
	for (Train *part : world.parts) part->grf_cache.cache_valid = 0x1F;
	Json parameters = world.Parameters();
	Json stale = parameters;
	stale["part_id"]["generation"] = stale["part_id"]["generation"].get<uint64_t>() + 1;
	REQUIRE_THROWS_AS(QueryVehicleDetailsState("vehicle.refit_subtypes", stale), InvalidParameters);
	REQUIRE_THROWS_AS(ResolveVehicleRefitLabelString(stale["vehicle_id"], stale["part_id"], to_underlying(world.cargo), 1), InvalidParameters);
	REQUIRE_THROWS_AS(ResolveVehicleRefitLabelString(parameters["vehicle_id"], parameters["part_id"], to_underlying(world.cargo), UINT8_MAX), InvalidParameters);

	const Randomizer before = _random;
	const Randomizer before_interactive = _interactive_random;
	const auto random_bits = world.tail->random_bits;
	const auto random_triggers = world.tail->waiting_random_triggers;
	auto first = QueryVehicleDetailsState("vehicle.refit_subtypes", parameters);
	REQUIRE(first.has_value());
	REQUIRE(first->at("refittable").get<bool>());
	REQUIRE(first->at("current_cargo").get<bool>());
	REQUIRE(first->at("keep_option").get<bool>());
	REQUIRE(first->at("probe_cap_reached").get<bool>());
	REQUIRE_FALSE(first->at("complete").get<bool>());
	REQUIRE(first->at("subtypes").size() == 16);
	REQUIRE(first->at("next_after_subtype") == 15);
	REQUIRE(first->at("subtypes").at(0).at("label_truncated").get<bool>());
	REQUIRE(first->at("subtypes").at(0).at("label").get<std::string>().size() <= 128);
	REQUIRE(world.tail->cargo_type == world.cargo);
	REQUIRE(world.tail->cargo_subtype == 17);
	REQUIRE(world.tail->random_bits == random_bits);
	REQUIRE(world.tail->waiting_random_triggers == random_triggers);
	REQUIRE(_random.state[0] == before.state[0]);
	REQUIRE(_random.state[1] == before.state[1]);
	REQUIRE(_interactive_random.state[0] == before_interactive.state[0]);
	REQUIRE(_interactive_random.state[1] == before_interactive.state[1]);
	for (Train *part : world.parts) REQUIRE(part->grf_cache.cache_valid == 0);

	/* A cheap no-callback response neither consumes nor needs the probe gate. */
	world.engine->info.refit_mask.Reset(world.cargo);
	auto not_refittable = QueryVehicleDetailsState("vehicle.refit_subtypes", parameters);
	REQUIRE(not_refittable.has_value());
	REQUIRE_FALSE(not_refittable->at("refittable").get<bool>());
	REQUIRE_FALSE(not_refittable->at("keep_option").get<bool>());
	world.engine->info.refit_mask.Set(world.cargo);
	world.engine->info.callback_mask.Reset(VehicleCallbackMask::CargoSuffix);
	auto keep_only = QueryVehicleDetailsState("vehicle.refit_subtypes", parameters);
	REQUIRE(keep_only.has_value());
	REQUIRE(keep_only->at("refittable").get<bool>());
	REQUIRE(keep_only->at("keep_option").get<bool>());
	REQUIRE(keep_only->at("subtypes").empty());
	world.engine->info.callback_mask.Set(VehicleCallbackMask::CargoSuffix);
	try {
		(void)QueryVehicleDetailsState("vehicle.refit_subtypes", parameters);
		FAIL("Immediate second callback scan should be rate limited");
	} catch (const InvalidParameters &error) {
		REQUIRE(std::string_view(error.what()).find("World text observation unavailable") != std::string_view::npos);
	}
	try {
		(void)ResolveVehicleRefitLabelString(parameters["vehicle_id"], parameters["part_id"], to_underlying(world.cargo), 5);
		FAIL("Full-label probe should share enumeration's admission gate");
	} catch (const InvalidParameters &error) {
		REQUIRE(std::string_view(error.what()).find("World text observation unavailable") != std::string_view::npos);
	}

	std::this_thread::sleep_for(std::chrono::milliseconds(1100));
	Json text_request = {{"kind", "vehicle_refit_label"}, {"target", parameters["part_id"]},
		{"vehicle_id", parameters["vehicle_id"]}, {"cargo_id", to_underlying(world.cargo)}, {"subtype", 5}, {"limit", 128}};
	auto text_first = QueryWorldDetailsState("display.text", text_request);
	REQUIRE(text_first.has_value());
	REQUIRE(text_first->at("kind") == "vehicle_refit_label");
	REQUIRE(text_first->at("target") == parameters["part_id"]);
	REQUIRE(text_first->at("vehicle_id") == parameters["vehicle_id"]);
	REQUIRE(text_first->at("next_after_byte") == 127);
	REQUIRE(text_first->at("content_version").get<std::string>().size() == 64);
	REQUIRE_THROWS_AS(QueryVehicleDetailsState("vehicle.refit_subtypes", parameters), InvalidParameters);
	std::this_thread::sleep_for(std::chrono::milliseconds(1100));
	text_request["after_byte"] = text_first->at("next_after_byte");
	text_request["expected_content_version"] = text_first->at("content_version");
	auto text_second = QueryWorldDetailsState("display.text", text_request);
	REQUIRE(text_second.has_value());
	REQUIRE(text_second->at("next_after_byte").is_null());
	REQUIRE(text_first->at("text").get<std::string>() + text_second->at("text").get<std::string>() == GetString(world.label_id));
	REQUIRE_THROWS_AS(ResolveVehicleRefitLabelString(parameters["vehicle_id"], parameters["part_id"], to_underlying(world.cargo), 5), InvalidParameters);

	std::this_thread::sleep_for(std::chrono::milliseconds(1100));
	const StringID selected = ResolveVehicleRefitLabelString(parameters["vehicle_id"], parameters["part_id"], to_underlying(world.cargo), 5);
	REQUIRE(selected == world.label_id);
	REQUIRE(world.tail->cargo_type == world.cargo);
	REQUIRE(world.tail->cargo_subtype == 17);
	for (Train *part : world.parts) REQUIRE(part->grf_cache.cache_valid == 0);
	REQUIRE(_random.state[0] == before.state[0]);
	REQUIRE(_random.state[1] == before.state[1]);
	REQUIRE_THROWS_AS(QueryVehicleDetailsState("vehicle.refit_subtypes", parameters), InvalidParameters);

	std::this_thread::sleep_for(std::chrono::milliseconds(1100));
	world.SetCallbackResult(0x400); // GUI's first-candidate termination value.
	try {
		(void)ResolveVehicleRefitLabelString(parameters["vehicle_id"], parameters["part_id"], to_underlying(world.cargo), 1);
		FAIL("A subtype after the first STR_EMPTY must stay hidden");
	} catch (const InvalidParameters &error) {
		REQUIRE(std::string_view(error.what()).find("subtype is unavailable") != std::string_view::npos);
	}
	REQUIRE(world.tail->cargo_type == world.cargo);
	REQUIRE(world.tail->cargo_subtype == 17);
	for (Train *part : world.parts) REQUIRE(part->grf_cache.cache_valid == 0);

	/* Validation precedes the rate gate, even after an admitted scan. */
	world.AddPart();
	REQUIRE(world.parts.size() == 1025);
	try {
		(void)QueryVehicleDetailsState("vehicle.refit_subtypes", world.Parameters());
		FAIL("A consist past the native 1024-part bound should be rejected");
	} catch (const InvalidParameters &error) {
		REQUIRE(std::string_view(error.what()).find("native train-length bound") != std::string_view::npos);
	}
}

} // namespace
