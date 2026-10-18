/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file observations_industries.cpp Bounded callback-free public industry observations. */

#include "../stdafx.h"
#include "actions_internal.h"
#include "../cargotype.h"
#include "../core/math_func.hpp"
#include "../core/string_builder.hpp"
#include "../core/utf8.hpp"
#include "../industry.h"
#include "../misc/history_func.hpp"
#include "../string_func.h"
#include "../strings_func.h"
#include "../tile_map.h"
#include "../timer/timer_game_economy.h"
#include "../town.h"

#include <algorithm>
#include <array>
#include <string_view>

#include "../table/strings.h"

#include "../safeguards.h"

namespace Integration::Detail {
namespace {

constexpr size_t INDUSTRY_REPORT_BYTES = 40 * 1024;
constexpr size_t INDUSTRY_NAME_BYTES = 256;

Json BoundedIndustryReport(Json result)
{
	if (result.dump().size() > INDUSTRY_REPORT_BYTES) throw InvalidParameters("Industry report exceeds the observation byte budget");
	return result;
}

struct IndustryNamePreview {
	std::string text;
	bool truncated = false;
};

struct NameClip {
	size_t length;
	bool truncated;
};

constexpr NameClip ClipIndustryName(std::string_view full, size_t byte_limit)
{
	size_t length = std::min(full.size(), byte_limit);
	while (length > 0 && length < full.size() && (static_cast<uint8_t>(full[length]) & 0xC0) == 0x80) --length;
	return {length, length < full.size()};
}

constexpr auto LONG_UTF8_NAME = [] {
	std::array<char, 258> value{};
	for (size_t i = 0; i < 255; ++i) value[i] = 'a';
	value[255] = static_cast<char>(0xC3);
	value[256] = static_cast<char>(0xA9);
	value[257] = 'z';
	return value;
}();
static_assert(ClipIndustryName({LONG_UTF8_NAME.data(), LONG_UTF8_NAME.size()}, 256).length == 255);
static_assert(ClipIndustryName({LONG_UTF8_NAME.data(), LONG_UTF8_NAME.size()}, 256).truncated);
static_assert(ClipIndustryName({LONG_UTF8_NAME.data(), LONG_UTF8_NAME.size()}, 258).length == 258);
static_assert(!ClipIndustryName({LONG_UTF8_NAME.data(), LONG_UTF8_NAME.size()}, 258).truncated);

/** Keep plain valid Unicode, then clip without splitting a UTF-8 code point. */
IndustryNamePreview PlainIndustryName(std::string value)
{
	std::string valid = StrMakeValid(std::move(value), StringValidationSetting::AllowNewline);
	std::string plain;
	StringBuilder builder(plain);
	for (char32_t c : Utf8View(valid)) {
		if (IsTextDirectionChar(c)) continue;
		/* The public name is one line, even when a GRF string contains a break. */
		if (c == '\n') c = ' ';
		builder.PutUtf8(c);
	}
	NameClip clip = ClipIndustryName(plain, INDUSTRY_NAME_BYTES);
	return {plain.substr(0, clip.length), clip.truncated};
}

/** The normal company may inspect any industry; generation and boot fence target reuse. */
const Industry *CheckedIndustry(const Json &parameters)
{
	const Json &handle = parameters.at("industry_id");
	if (!handle.is_object() || handle.size() != 3 || !handle.contains("id") ||
			!handle.contains("generation") || !handle.contains("engine_instance_id") ||
			!handle.at("id").is_number_integer() || !handle.at("generation").is_number_integer() ||
			!handle.at("engine_instance_id").is_string()) throw InvalidParameters("Invalid industry handle");
	Parameters fields(handle);
	const uint32_t raw_id = static_cast<uint32_t>(fields.Int("id", 0, 63999));
	const std::string instance = fields.Text("engine_instance_id", "", 256);
	if (instance.empty() || instance != InstanceId()) throw InvalidParameters("Industry target belongs to another engine instance");
	if (!handle.at("generation").is_number_unsigned() && handle.at("generation").get<int64_t>() <= 0) throw InvalidParameters("Invalid industry generation");
	const uint64_t generation = handle.at("generation").get<uint64_t>();
	const Industry *industry = Industry::GetIfValid(IndustryID(raw_id));
	if (industry == nullptr || industry->GetInstanceGeneration() != generation) throw InvalidParameters("Industry target is stale");
	if (industry->type >= NUM_INDUSTRYTYPES || industry->location.w == 0 || industry->location.h == 0 || !IsValidTile(industry->location.tile)) throw InvalidParameters("Industry target is unavailable");
	if (industry->accepted.size() > INDUSTRY_NUM_INPUTS || industry->produced.size() > INDUSTRY_NUM_OUTPUTS) throw InvalidParameters("Industry cargo slots exceed native bounds");
	return industry;
}

const char *PeriodUnit()
{
	return TimerGameEconomy::UsingWallclockUnits() ? "minute" : "month";
}

bool CurrentCargo(CargoType cargo)
{
	return cargo < NUM_CARGO && CargoSpec::Get(cargo)->IsValid();
}

Json IndustryList(const Json &parameters)
{
	if (!parameters.is_object()) throw InvalidParameters("Invalid industries parameters");
	for (auto it = parameters.begin(); it != parameters.end(); ++it) {
		if (it.key() != "after_id" && it.key() != "limit") throw InvalidParameters("Unexpected industries parameter");
	}
	Parameters fields(parameters);
	/* Preserve the published generic list cursor range, including empty pages beyond this pool. */
	const int64_t after = fields.Int("after_id", -1, UINT32_MAX, -1);
	const size_t limit = static_cast<size_t>(fields.Int("limit", 1, 256, 64));
	uint64_t index = static_cast<uint64_t>(after + 1);
	int64_t last_consumed = after;
	size_t scanned = 0;
	Json result = SnapshotBase();
	result["items"] = Json::array();
	result["next_after_id"] = nullptr;
	while (index < Industry::GetPoolSize() && scanned < 4096 && result["items"].size() < limit) {
		const Industry *industry = Industry::GetIfValid(index);
		++scanned;
		if (industry != nullptr) {
			if (industry->type >= NUM_INDUSTRYTYPES || industry->location.w == 0 || industry->location.h == 0 || !IsValidTile(industry->location.tile) ||
					industry->accepted.size() > INDUSTRY_NUM_INPUTS || industry->produced.size() > INDUSTRY_NUM_OUTPUTS) {
				throw InvalidParameters("Industry list item is unavailable");
			}
			const IndustrySpec *spec = GetIndustrySpec(industry->type);
			Json accepted = Json::array(), produced = Json::array();
			for (const auto &cargo : industry->accepted) if (CurrentCargo(cargo.cargo)) accepted.push_back(to_underlying(cargo.cargo));
			for (const auto &cargo : industry->produced) if (CurrentCargo(cargo.cargo)) produced.push_back({
				{"cargo_id", to_underlying(cargo.cargo)}, {"waiting", cargo.waiting}, {"production_rate", cargo.rate}});
			IndustryNamePreview name = PlainIndustryName(industry->town == nullptr ? GetString(spec->name) : GetString(STR_INDUSTRY_NAME, industry->index));
			Json item = {{"industry_id", industry->index.base()}, {"industry", Handle(industry)},
				{"name", std::move(name.text)}, {"name_truncated", name.truncated},
				{"location", TileCoordinates(industry->location.tile)}, {"width", industry->location.w}, {"height", industry->location.h},
				{"accepted_cargo_ids", std::move(accepted)}, {"produced_cargo", std::move(produced)}};
			Json trial = result;
			trial["items"].push_back(item);
			trial["next_after_id"] = index;
			if (trial.dump().size() > INDUSTRY_REPORT_BYTES) {
				if (result["items"].empty()) throw InvalidParameters("One industry list item exceeds the observation byte budget");
				break; // Do not consume this item: the next page must see it.
			}
			result["items"].push_back(std::move(item));
		}
		last_consumed = static_cast<int64_t>(index);
		++index;
	}
	result["next_after_id"] = index < Industry::GetPoolSize() ? Json(last_consumed) : Json(nullptr);
	return BoundedIndustryReport(std::move(result));
}

/** True means the immediately preceding economy period has completed data. */
bool LastPeriodValid(const Industry *industry)
{
	return IsValidHistory(industry->valid_history, HISTORY_MONTH, 0);
}

const char *InputWaitingVisibility(const IndustrySpec *spec, size_t slot)
{
	const bool stockpiling = spec->callback_mask.Any({IndustryCallbackMask::ProductionCargoArrival, IndustryCallbackMask::Production256Ticks});
	if (!stockpiling) return "hidden";
	if (!spec->behaviour.Test(IndustryBehaviour::CargoTypesUnlimited) && slot >= INDUSTRY_ORIGINAL_NUM_INPUTS) return "hidden";
	if (spec->callback_mask.Test(IndustryCallbackMask::CargoSuffix)) return "unknown_callback";
	return "visible";
}

Json IndustryDetails(const Json &parameters)
{
	if (!parameters.is_object() || parameters.size() != 1) throw InvalidParameters("Unexpected industry details parameters");
	const Industry *industry = CheckedIndustry(parameters);
	const IndustrySpec *spec = GetIndustrySpec(industry->type);
	if (spec == nullptr) throw InvalidParameters("Industry type is unavailable");
	Json result = SnapshotBase();
	result["industry_id"] = Handle(industry);
	IndustryNamePreview name = PlainIndustryName(industry->town == nullptr ? GetString(spec->name) : GetString(STR_INDUSTRY_NAME, industry->index));
	result["name"] = std::move(name.text);
	result["name_truncated"] = name.truncated;
	result["industry_type"] = industry->type;
	result["location"] = TileCoordinates(industry->location.tile);
	result["width"] = industry->location.w;
	result["height"] = industry->location.h;
	result["nearest_town"] = industry->town == nullptr ? Json(nullptr) : Handle(industry->town);
	result["announced_closure"] = industry->prod_level == PRODLEVEL_CLOSURE;
	result["period_unit"] = PeriodUnit();
	result["accepted_cargo_ids"] = Json::array();
	result["produced_cargo"] = Json::array();
	result["accepted"] = Json::array();
	result["produced"] = Json::array();
	result["omitted_presentation"] = {{"cargo_suffix_callback", spec->callback_mask.Test(IndustryCallbackMask::CargoSuffix)},
		{"window_more_text_callback", spec->callback_mask.Test(IndustryCallbackMask::WindowMoreText)},
		{"custom_text", !industry->text.empty()}};
	const bool valid_last = LastPeriodValid(industry);
	for (size_t slot = 0; slot < industry->accepted.size(); ++slot) {
		const auto &entry = industry->accepted[slot];
		if (!CurrentCargo(entry.cargo)) continue;
		result["accepted_cargo_ids"].push_back(to_underlying(entry.cargo));
		const char *visibility = InputWaitingVisibility(spec, slot);
		Json waiting = std::string_view(visibility) == "visible" ? Json(entry.waiting) : Json(nullptr);
		Json delivered = nullptr, waiting_average = nullptr;
		if (valid_last) {
			const Industry::AcceptedHistory history = entry.history == nullptr ? Industry::AcceptedHistory{} : (*entry.history)[LAST_MONTH];
			delivered = history.accepted;
			waiting_average = history.waiting;
		}
		result["accepted"].push_back({{"slot", slot}, {"cargo_id", to_underlying(entry.cargo)},
			{"input_waiting", waiting}, {"input_waiting_visibility", visibility},
			{"input_waiting_unavailable", std::string_view(visibility) == "unknown_callback"},
			{"delivered_last_period", delivered}, {"waiting_average_last_period", waiting_average}});
	}
	for (size_t slot = 0; slot < industry->produced.size(); ++slot) {
		const auto &entry = industry->produced[slot];
		if (!CurrentCargo(entry.cargo)) continue;
		result["produced_cargo"].push_back({{"cargo_id", to_underlying(entry.cargo)},
			{"waiting", entry.waiting}, {"production_rate", entry.rate}});
		const auto &history = entry.history[LAST_MONTH];
		result["produced"].push_back({{"slot", slot}, {"cargo_id", to_underlying(entry.cargo)},
			{"produced_last_period", valid_last ? Json(history.production) : Json(nullptr)},
			{"transported_last_period", valid_last ? Json(history.transported) : Json(nullptr)},
			{"transported_percent_last_period", valid_last ? Json(ToPercent8(history.PctTransported())) : Json(nullptr)}});
	}
	return BoundedIndustryReport(std::move(result));
}

Json IndustryMonthlyStats(const Json &parameters)
{
	if (!parameters.is_object() || parameters.size() != 3) throw InvalidParameters("Unexpected industry history parameters");
	const Industry *industry = CheckedIndustry(parameters);
	Parameters fields(parameters);
	const CargoType cargo = static_cast<CargoType>(fields.Int("cargo_id", 0, NUM_CARGO - 1));
	if (!CargoSpec::Get(cargo)->IsValid()) throw InvalidParameters("Cargo is unavailable");
	const std::string direction = fields.Text("direction", "", 8);
	if (direction != "accepted" && direction != "produced") throw InvalidParameters("Invalid industry history direction");
	const Industry::AcceptedCargo *accepted = nullptr;
	const Industry::ProducedCargo *produced = nullptr;
	if (direction == "accepted") {
		for (const auto &entry : industry->accepted) {
			if (!CurrentCargo(entry.cargo) || entry.cargo != cargo) continue;
			if (accepted != nullptr) throw InvalidParameters("Cargo matches multiple accepted slots");
			accepted = &entry;
		}
		if (accepted == nullptr) throw InvalidParameters("Cargo is not accepted here");
	} else {
		for (const auto &entry : industry->produced) {
			if (!CurrentCargo(entry.cargo) || entry.cargo != cargo) continue;
			if (produced != nullptr) throw InvalidParameters("Cargo matches multiple produced slots");
			produced = &entry;
		}
		if (produced == nullptr) throw InvalidParameters("Cargo is not produced here");
	}
	Json result = SnapshotBase();
	result["industry_id"] = Handle(industry);
	result["cargo_id"] = to_underlying(cargo);
	result["direction"] = direction;
	result["period_unit"] = PeriodUnit();
	result["points"] = Json::array();
	for (uint age = 0; age < HISTORY_PERIODS; ++age) {
		Json point = {{"periods_ago", age + 1}};
		if (accepted != nullptr) {
			Industry::AcceptedHistory history{};
			const bool valid = accepted->history != nullptr ?
				GetHistory(*accepted->history, industry->valid_history, HISTORY_MONTH, age, history) :
				IsValidHistory(industry->valid_history, HISTORY_MONTH, age);
			point["delivered"] = valid ? Json(history.accepted) : Json(nullptr);
			point["waiting_average"] = valid ? Json(history.waiting) : Json(nullptr);
		} else {
			Industry::ProducedHistory history{};
			const bool valid = GetHistory(produced->history, industry->valid_history, HISTORY_MONTH, age, history);
			point["produced"] = valid ? Json(history.production) : Json(nullptr);
			point["transported"] = valid ? Json(history.transported) : Json(nullptr);
		}
		result["points"].push_back(std::move(point));
	}
	return BoundedIndustryReport(std::move(result));
}

} // namespace

std::optional<Json> QueryIndustryDetailsState(const std::string &query, const Json &parameters)
{
	if (query == "industries") return IndustryList(parameters);
	if (query == "industry.details") return IndustryDetails(parameters);
	if (query == "industry.monthly_stats") return IndustryMonthlyStats(parameters);
	return std::nullopt;
}

} // namespace Integration::Detail
