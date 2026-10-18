/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file test_integration_display_text.cpp Native display-text continuation and admission regressions. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions_internal.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../core/endian_func.hpp"
#include "../goal_base.h"
#include "../industry.h"
#include "../language.h"
#include "../strings_func.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <thread>

#include "table/strings.h"

#include "../safeguards.h"

namespace {
using namespace Integration;
using namespace Integration::Detail;

struct DisplayTextWorld {
	CompanyID previous_company = _current_company;
	Company *own;
	Company *other;
	Goal *goal;
	Goal *foreign;
	Industry *industry;

	DisplayTextWorld()
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
		_goal_pool.CleanPool();
		_industry_pool.CleanPool();
		_company_pool.CleanPool();
		Map::Allocate(64, 64);
		REQUIRE(Company::CanAllocateItem(2));
		this->own = Company::Create();
		this->other = Company::Create();
		_current_company = this->own->index;
		REQUIRE(Goal::CanAllocateItem(2));
		this->goal = Goal::Create();
		this->goal->company = this->own->index;
		this->foreign = Goal::Create();
		this->foreign->company = this->other->index;
		REQUIRE(Industry::CanAllocateItem());
		this->industry = Industry::Create(TileXY(10, 10));
		this->industry->location.w = 1;
		this->industry->location.h = 1;
	}

	~DisplayTextWorld()
	{
		_goal_pool.CleanPool();
		_industry_pool.CleanPool();
		_company_pool.CleanPool();
		_current_company = this->previous_company;
	}
};

void WaitForTextGate() { std::this_thread::sleep_for(std::chrono::milliseconds(1100)); }
} // namespace

TEST_CASE("Display text continuation preserves full content and shares preview admission", "[integration][display-text]")
{
	DisplayTextWorld world;
	WaitForTextGate(); // The shared admission stamp can be set by another Catch case.
	const Json industry_target = Handle(world.industry);
	world.industry->location.w = 0;
	CHECK_THROWS_AS(QueryWorldDetailsState("display.text", Json{{"kind", "industry_name"}, {"target", industry_target}}), InvalidParameters);
	CHECK_THROWS_AS(QueryIndustryDetailsState("industry.details", Json{{"industry_id", industry_target}}), InvalidParameters);
	world.industry->location.w = 1;
	world.industry->location.h = 0;
	CHECK_THROWS_AS(QueryWorldDetailsState("display.text", Json{{"kind", "industry_name"}, {"target", industry_target}}), InvalidParameters);
	CHECK_THROWS_AS(QueryIndustryDetailsState("industry.details", Json{{"industry_id", industry_target}}), InvalidParameters);
	world.industry->location.h = 1;
	world.industry->accepted.emplace_back();
	world.industry->accepted.front().cargo = NUM_CARGO; // Non-sentinel, out of the cargo spec array.
	CHECK_THROWS_AS(QueryWorldDetailsState("display.text", Json{{"kind", "industry_custom_text"}, {"target", industry_target}}), InvalidParameters);
	world.industry->accepted.clear();
	world.industry->produced.emplace_back();
	world.industry->produced.front().cargo = NUM_CARGO;
	CHECK_THROWS_AS(QueryWorldDetailsState("display.text", Json{{"kind", "industry_extra_text"}, {"target", industry_target}}), InvalidParameters);
	world.industry->produced.clear();
	std::string text(2047, 'A');
	text += "\xE2\x82\xAC";
	text.append(300, 'B');
	world.goal->text = GetEncodedString(STR_JUST_RAW_STRING, std::string_view(text));
	world.foreign->text = GetEncodedString(STR_JUST_RAW_STRING, std::string_view("private text"));
	Json target = Handle(world.goal);
	Json parameters = {{"kind", "goal_text"}, {"target", target}, {"limit", 2048}};

	const auto first = QueryWorldDetailsState("display.text", parameters);
	REQUIRE(first.has_value());
	CHECK((*first)["text"].get<std::string>() == std::string(2047, 'A'));
	CHECK((*first)["next_after_byte"] == 2047);
	CHECK((*first)["total_bytes"] == 2350);
	/* Independent Python hashlib.blake2b(text, digest_size=32) reference. */
	CHECK((*first)["content_version"] == "e43fc92e8a98e8429bf6436271b4a03fa7d929219cfac77a8d531bc12fb07303");
	CHECK_THROWS_AS(QueryWorldDetailsState("goals", Json::object()), InvalidParameters);

	WaitForTextGate();
	CHECK_THROWS_AS(QueryWorldDetailsState("display.text", Json{{"kind", "goal_text"}, {"target", Handle(world.foreign)}}), InvalidParameters);
	parameters["after_byte"] = 2047;
	parameters["expected_content_version"] = (*first)["content_version"];
	const auto second = QueryWorldDetailsState("display.text", parameters);
	REQUIRE(second.has_value());
	CHECK((*second)["text"].get<std::string>() == text.substr(2047));
	CHECK((*second)["next_after_byte"].is_null());
	CHECK((*second)["content_version"] == (*first)["content_version"]);
	CHECK_THROWS_AS(QueryWorldDetailsState("display.text", parameters), InvalidParameters);

	WaitForTextGate();
	parameters["after_byte"] = 2048; // Inside the three-byte Euro sign.
	CHECK_THROWS_AS(QueryWorldDetailsState("display.text", parameters), InvalidParameters);
	WaitForTextGate();
	world.goal->text = GetEncodedString(STR_JUST_RAW_STRING, std::string_view("changed within tick"));
	parameters["after_byte"] = 2047;
	CHECK_THROWS_AS(QueryWorldDetailsState("display.text", parameters), InvalidParameters);
	WaitForTextGate();
	world.industry->text = GetEncodedString(STR_JUST_RAW_STRING, std::string_view("Visible industry note"));
	Json industry_text = QueryWorldDetailsState("display.text", Json{{"kind", "industry_custom_text"}, {"target", Handle(world.industry)}}).value();
	CHECK(industry_text["text"] == "Visible industry note");
	CHECK(industry_text["target"] == Handle(world.industry));
	CHECK_THROWS_AS(QueryWorldDetailsState("goals", Json::object()), InvalidParameters);

	/* Stored text can exceed the former 64 KiB whole-render proposal. */
	WaitForTextGate();
	std::string very_long(110000, 'Z');
	world.goal->text = GetEncodedString(STR_JUST_RAW_STRING, std::string_view(very_long));
	Json long_request = {{"kind", "goal_text"}, {"target", target}};
	const auto long_first = QueryWorldDetailsState("display.text", long_request);
	REQUIRE(long_first.has_value());
	CHECK((*long_first)["total_bytes"] == 110000);
	CHECK((*long_first)["text"].get<std::string>() == std::string(2048, 'Z'));
	CHECK((*long_first)["next_after_byte"] == 2048);
	WaitForTextGate();
	long_request["after_byte"] = 100000;
	long_request["expected_content_version"] = (*long_first)["content_version"];
	const auto deep = QueryWorldDetailsState("display.text", long_request);
	REQUIRE(deep.has_value());
	CHECK((*deep)["text"].get<std::string>() == std::string(2048, 'Z'));
	CHECK((*deep)["next_after_byte"] == 102048);
	CHECK((*deep)["content_version"] == (*long_first)["content_version"]);
}
