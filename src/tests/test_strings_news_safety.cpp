/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License, version 2.
 */

/** @file test_strings_news_safety.cpp Native formatting guards for late-decoded presentation text. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../core/endian_func.hpp"
#include "../core/pool_func.hpp"
#include "../core/string_builder.hpp"
#include "../depot_base.h"
#include "../language.h"
#include "../settings_type.h"
#include "../station_base.h"
#include "../strings_func.h"
#include "../town.h"

#include <fstream>

#include "../table/control_codes.h"
#include "table/strings.h"

#include "../safeguards.h"

namespace {

struct StringSafetyFormatting {
	std::string previous_group_separator = _settings_game.locale.digit_group_separator;
	std::string previous_decimal_separator = _settings_game.locale.digit_decimal_separator;

	StringSafetyFormatting()
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
		_settings_game.locale.digit_group_separator = ",";
		_settings_game.locale.digit_decimal_separator = ".";
	}

	~StringSafetyFormatting()
	{
		_settings_game.locale.digit_group_separator = this->previous_group_separator;
		_settings_game.locale.digit_decimal_separator = this->previous_decimal_separator;
	}
};

struct DepotNameFormatting : StringSafetyFormatting {
	DepotNameFormatting()
	{
		_depot_pool.CleanPool();
		_station_pool.CleanPool();
		_town_pool.CleanPool();
	}

	~DepotNameFormatting()
	{
		_depot_pool.CleanPool();
		_station_pool.CleanPool();
		_town_pool.CleanPool();
	}
};

/** Exercise the owned encoded-string decoder, with another argument after the guarded code. */
std::string FormatWithTrailingNumber(StringControlCode code, uint64_t value, uint64_t argument)
{
	std::string format;
	StringBuilder builder(format);
	builder.PutUtf8(code);
	builder.PutChar('|');
	builder.PutUtf8(SCC_NUM);
	return GetEncodedString(STR_JUST_RAW_STRING, format, value, argument, uint64_t{37}).GetDecodedString();
}

} // namespace

TEST_CASE("Native DECIMAL preserves supported precision and following arguments", "[strings][news-safety]")
{
	StringSafetyFormatting fixture;
	for (uint64_t digits = 0; digits <= 18; ++digits) {
		CAPTURE(digits);
		const std::string expected = digits == 0 ? "0|37" : "0." + std::string(digits, '0') + "|37";
		CHECK(FormatWithTrailingNumber(SCC_DECIMAL, 0, digits) == expected);
	}
	CHECK(FormatWithTrailingNumber(SCC_DECIMAL, 12345, 0) == "12,345|37");
	CHECK(FormatWithTrailingNumber(SCC_DECIMAL, 12345, 2) == "123.45|37");
	CHECK(FormatWithTrailingNumber(SCC_DECIMAL, 1'000'000'000'000'000'000, 18) == "1.000000000000000000|37");
}

TEST_CASE("Native DECIMAL rejects invalid precision before arithmetic or narrowing", "[strings][news-safety]")
{
	StringSafetyFormatting fixture;
	for (uint64_t digits : {static_cast<uint64_t>(-1), uint64_t{19}, uint64_t{21}, (uint64_t{1} << 32) + 2}) {
		CAPTURE(digits);
		CHECK(FormatWithTrailingNumber(SCC_DECIMAL, 12345, digits) == "(invalid parameter)|37");
	}
}

TEST_CASE("Native DEPOT preserves valid names for all four transport types", "[strings][news-safety]")
{
	DepotNameFormatting fixture;
	REQUIRE(Town::CanAllocateItem());
	Town *town = Town::Create();
	town->name = "Formatter town";
	REQUIRE(Depot::CanAllocateItem());
	Depot *depot = Depot::Create();
	depot->town = town;
	const std::array<std::pair<VehicleType, StringID>, 3> templates{{
		{VehicleType::Train, STR_FORMAT_DEPOT_NAME_TRAIN},
		{VehicleType::Road, STR_FORMAT_DEPOT_NAME_ROAD_VEHICLE},
		{VehicleType::Ship, STR_FORMAT_DEPOT_NAME_SHIP},
	}};
	for (auto [type, name] : templates) {
		CAPTURE(type);
		CHECK(FormatWithTrailingNumber(SCC_DEPOT_NAME, to_underlying(type), depot->index.base()) == GetString(name, town->index) + "|37");
		depot->town_cn = 2;
		CHECK(FormatWithTrailingNumber(SCC_DEPOT_NAME, to_underlying(type), depot->index.base()) == GetString(name + 1, town->index, 3) + "|37");
		depot->town_cn = 0;
	}
	depot->name = "Named depot";
	depot->town = nullptr;
	for (auto [type, name] : templates) {
		CHECK(FormatWithTrailingNumber(SCC_DEPOT_NAME, to_underlying(type), depot->index.base()) == "Named depot|37");
	}
	REQUIRE(Station::CanAllocateItem());
	Station *station = Station::Create();
	station->name = "Formatter airport";
	CHECK(FormatWithTrailingNumber(SCC_DEPOT_NAME, to_underlying(VehicleType::Aircraft), station->index.base()) ==
			GetString(STR_FORMAT_DEPOT_NAME_AIRCRAFT, station->index) + "|37");
}

TEST_CASE("Native DEPOT safely consumes invalid types and stale or incomplete names", "[strings][news-safety]")
{
	DepotNameFormatting fixture;
	REQUIRE(Depot::CanAllocateItem());
	Depot *depot = Depot::Create();
	const DepotID depot_id = depot->index;
	depot->name = "Named depot";
	for (uint64_t type : {uint64_t{4}, uint64_t{255}, uint64_t{256}, uint64_t{259}}) {
		CAPTURE(type);
		CHECK(FormatWithTrailingNumber(SCC_DEPOT_NAME, type, depot_id.base()) == "|37");
	}
	depot->name.clear(); // A generated name with no town must remain safe.
	for (VehicleType type : {VehicleType::Train, VehicleType::Road, VehicleType::Ship}) {
		CHECK(FormatWithTrailingNumber(SCC_DEPOT_NAME, to_underlying(type), depot_id.base()) == "|37");
	}
	_depot_pool.CleanPool();
	CHECK_FALSE(Depot::IsValidID(depot_id));
	for (VehicleType type : {VehicleType::Train, VehicleType::Road, VehicleType::Ship}) {
		CHECK(FormatWithTrailingNumber(SCC_DEPOT_NAME, to_underlying(type), depot_id.base()) == "|37");
		CHECK(FormatWithTrailingNumber(SCC_DEPOT_NAME, to_underlying(type), DepotID::Invalid().base()) == "|37");
	}
	REQUIRE(Station::CanAllocateItem());
	const StationID station_id = Station::Create()->index;
	_station_pool.CleanPool();
	CHECK_FALSE(Station::IsValidID(station_id));
	const std::string aircraft_name = FormatWithTrailingNumber(SCC_DEPOT_NAME, to_underlying(VehicleType::Aircraft), station_id.base());
	CHECK(aircraft_name == GetString(STR_FORMAT_DEPOT_NAME_AIRCRAFT, station_id) + "|37");
	CHECK(aircraft_name.find(GetString(STR_UNKNOWN_STATION)) != std::string::npos);
}

TEST_CASE("Native DEPOT rejects oversized IDs before narrowing to live slots", "[strings][news-safety]")
{
	DepotNameFormatting fixture;
	REQUIRE(Depot::CanAllocateItem());
	Depot *depot = Depot::Create();
	depot->name = "Live depot";
	for (uint64_t index : {uint64_t{65536} + depot->index.base(), UINT64_MAX, uint64_t{DepotID::End().base()}}) {
		CAPTURE(index);
		for (VehicleType type : {VehicleType::Train, VehicleType::Road, VehicleType::Ship}) {
			CHECK(FormatWithTrailingNumber(SCC_DEPOT_NAME, to_underlying(type), index) == "|37");
		}
	}
	REQUIRE(Station::CanAllocateItem());
	Station *station = Station::Create();
	station->name = "Live airport";
	const std::string unknown_hangar = GetString(STR_FORMAT_DEPOT_NAME_AIRCRAFT, StationID::Invalid()) + "|37";
	for (uint64_t index : {uint64_t{65536} + station->index.base(), UINT64_MAX, uint64_t{StationID::End().base()}}) {
		CAPTURE(index);
		const std::string aircraft_name = FormatWithTrailingNumber(SCC_DEPOT_NAME, to_underlying(VehicleType::Aircraft), index);
		CHECK(aircraft_name == unknown_hangar);
		CHECK(aircraft_name.find(station->name) == std::string::npos);
	}
}
