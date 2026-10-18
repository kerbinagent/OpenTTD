/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file observations_world_details.cpp Bounded public world and league observations. */

#include "../stdafx.h"
#include "actions_internal.h"
#include "actions_story.h"
#include "../cargotype.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../3rdparty/monocypher/monocypher.h"
#include "../core/string_builder.hpp"
#include "../core/utf8.hpp"
#include "../goal_base.h"
#include "../industry.h"
#include "../industry_presentation.h"
#include "../landscape.h"
#include "../league_base.h"
#include "../settings_type.h"
#include "../story_base.h"
#include "../string_func.h"
#include "../strings_func.h"
#include "../subsidy_base.h"
#include "../tile_map.h"
#include "../timer/timer_game_economy.h"
#include "../town.h"
#include "../town_cmd.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <functional>
#include <optional>
#include <string_view>
#include <vector>

#include "../table/strings.h"

#include "../safeguards.h"

namespace Integration::Detail {
namespace {

constexpr size_t WORLD_REPORT_BYTES = 40 * 1024;
constexpr size_t DISPLAY_TEXT_BYTES = 2048;
using WorldTextClock = std::chrono::steady_clock;
std::optional<WorldTextClock::time_point> last_world_text_start;

bool ValidWorldCargo(CargoType cargo)
{
	return to_underlying(cargo) < NUM_CARGO && CargoSpec::Get(cargo)->IsValid();
}

/** Shared by all whole-render world previews and the long-text reader. */
void AdmitWorldText()
{
	auto now = WorldTextClock::now();
	if (last_world_text_start.has_value() && now - *last_world_text_start < std::chrono::seconds(1)) throw InvalidParameters("World text observation unavailable");
	last_world_text_start = now;
}

Json BoundedWorld(Json result)
{
	if (result.dump().size() > WORLD_REPORT_BYTES) throw InvalidParameters("World report exceeds byte budget");
	return result;
}

/** The GUI renders decoded strings. Return only valid, plain UTF-8 text. */
std::string PlainText(std::string value)
{
	std::string valid = StrMakeValid(std::move(value), StringValidationSetting::AllowNewline);
	std::string plain;
	StringBuilder builder(plain);
	for (char32_t c : Utf8View(valid)) if (!IsTextDirectionChar(c)) builder.PutUtf8(c);
	return plain;
}

void PutDisplayText(Json &out, const char *key, std::string value)
{
	value = PlainText(std::move(value));
	size_t length = std::min(value.size(), DISPLAY_TEXT_BYTES);
	while (length > 0 && length < value.size() && (static_cast<uint8_t>(value[length]) & 0xC0) == 0x80) --length;
	out[key] = value.substr(0, length);
	out[std::string(key) + "_truncated"] = length < value.size();
}

std::string TextVersion(std::string_view text)
{
	std::array<uint8_t, 32> digest;
	crypto_blake2b(digest.data(), digest.size(), reinterpret_cast<const uint8_t *>(text.data()), text.size());
	static constexpr char hex[] = "0123456789abcdef";
	std::string result;
	result.reserve(digest.size() * 2);
	for (uint8_t byte : digest) {
		result.push_back(hex[byte >> 4]);
		result.push_back(hex[byte & 15]);
	}
	return result;
}

uint64_t UnsignedOffset(const Json &parameters)
{
	auto it = parameters.find("after_byte");
	if (it == parameters.end()) return 0;
	if (!it->is_number_integer()) throw InvalidParameters("Invalid text offset");
	if (it->is_number_unsigned()) return it->get<uint64_t>();
	int64_t value = it->get<int64_t>();
	if (value < 0) throw InvalidParameters("Invalid text offset");
	return static_cast<uint64_t>(value);
}

void CheckDisplayTextKeys(const Json &parameters, std::initializer_list<std::string_view> source_keys)
{
	for (auto it = parameters.begin(); it != parameters.end(); ++it) {
		std::string_view key = it.key();
		if (key == "kind" || key == "after_byte" || key == "limit" || key == "expected_content_version") continue;
		if (std::find(source_keys.begin(), source_keys.end(), key) == source_keys.end()) throw InvalidParameters("Invalid display text source parameters");
	}
}

template <typename T, uint32_t MAX_ID>
const T *CheckedHandle(const Json &parameters, const char *key)
{
	const Json &handle = parameters.at(key);
	if (!handle.is_object() || handle.size() != 3 || !handle.contains("id") || !handle.contains("generation") || !handle.contains("engine_instance_id") ||
			!handle.at("id").is_number_integer() || !handle.at("generation").is_number_integer() || !handle.at("engine_instance_id").is_string()) {
		throw InvalidParameters("Invalid target handle");
	}
	Parameters fields(handle);
	uint32_t id = static_cast<uint32_t>(fields.Int("id", 0, MAX_ID - 1));
	const std::string instance = fields.Text("engine_instance_id", "", 256);
	if (instance.empty() || instance != InstanceId()) throw InvalidParameters("Stale target handle");
	const Json &generation = handle.at("generation");
	if (!generation.is_number_unsigned() && generation.get<int64_t>() <= 0) throw InvalidParameters("Invalid target handle");
	uint64_t expected = generation.get<uint64_t>();
	const T *entity = T::GetIfValid(id);
	if (entity == nullptr || entity->GetInstanceGeneration() != expected) throw InvalidParameters("Stale or unavailable target");
	return entity;
}

/** A display source may replay earlier cargo callbacks, so fence every slot
 * before admitting the text read or invoking the GUI presentation helper. */
const Industry *CheckedDisplayIndustry(const Json &parameters)
{
	const Industry *industry = CheckedHandle<Industry, 64000>(parameters, "target");
	if (industry->type >= NUM_INDUSTRYTYPES || industry->location.w == 0 || industry->location.h == 0 || !IsValidTile(industry->location.tile)) {
		throw InvalidParameters("Industry target is unavailable");
	}
	if (industry->accepted.size() > INDUSTRY_NUM_INPUTS || industry->produced.size() > INDUSTRY_NUM_OUTPUTS) {
		throw InvalidParameters("Industry cargo slots exceed native bounds");
	}
	for (const auto &entry : industry->accepted) {
		if (entry.cargo != INVALID_CARGO && !ValidWorldCargo(entry.cargo)) throw InvalidParameters("Industry accepted cargo is unavailable");
	}
	for (const auto &entry : industry->produced) {
		if (entry.cargo != INVALID_CARGO && !ValidWorldCargo(entry.cargo)) throw InvalidParameters("Industry produced cargo is unavailable");
	}
	return industry;
}

template <typename T, uint32_t MAX_ID, typename Filter, typename Render>
Json WorldPage(const Parameters &p, Json result, Filter filter, Render render)
{
	uint64_t index = static_cast<uint64_t>(p.Int("after_id", -1, MAX_ID - 1, -1) + 1);
	const size_t limit = static_cast<size_t>(p.Int("limit", 1, 64, 32));
	result["items"] = Json::array();
	result["next_after_id"] = nullptr;
	uint32_t scanned = 0;
	while (index < T::GetPoolSize() && scanned < 4096 && result["items"].size() < limit) {
		const T *entity = T::GetIfValid(index);
		if (entity != nullptr && filter(entity)) {
			Json item = render(entity);
			if (!item.is_null()) {
				result["items"].push_back(std::move(item));
				result["next_after_id"] = index + 1 < T::GetPoolSize() ? Json(index) : Json(nullptr);
				if (result.dump().size() > WORLD_REPORT_BYTES) {
					result["items"].erase(result["items"].size() - 1);
					if (result["items"].empty()) throw InvalidParameters("World report row exceeds byte budget");
					break; // Retry this unconsumed slot on the next page.
				}
			}
		}
		++index;
		++scanned;
	}
	result["next_after_id"] = index < T::GetPoolSize() ? Json(index - 1) : Json(nullptr);
	if (result.dump().size() > WORLD_REPORT_BYTES) throw InvalidParameters("World report exceeds byte budget");
	return result;
}

bool VisibleStoryPage(const StoryPage *page, CompanyID audience)
{
	return page != nullptr && (page->company == CompanyID::Invalid() || page->company == audience);
}

Json LinkTarget(LinkType type, uint32_t id, CompanyID audience, bool global_story_only = false)
{
	Json target = {{"kind", "none"}, {"tile", nullptr}, {"entity", nullptr}};
	switch (type) {
		case LinkType::None: return target;
		case LinkType::Tile:
			if (!IsValidTile(TileIndex{id})) return nullptr;
			target["kind"] = "tile";
			target["tile"] = TileCoordinates(TileIndex{id});
			return target;
		case LinkType::Industry: {
			const Industry *industry = Industry::GetIfValid(id);
			if (industry == nullptr) return nullptr;
			target["kind"] = "industry";
			target["entity"] = Handle(industry);
			return target;
		}
		case LinkType::Town: {
			const Town *town = Town::GetIfValid(id);
			if (town == nullptr) return nullptr;
			target["kind"] = "town";
			target["entity"] = Handle(town);
			return target;
		}
		case LinkType::Company: {
			const Company *company = Company::GetIfValid(id);
			if (company == nullptr) return nullptr;
			target["kind"] = "company";
			target["entity"] = Handle(company);
			return target;
		}
		case LinkType::StoryPage: {
			const StoryPage *page = StoryPage::GetIfValid(id);
			if (!VisibleStoryPage(page, audience) || (global_story_only && page->company != CompanyID::Invalid())) return nullptr;
			target["kind"] = "story_page";
			target["entity"] = Handle(page);
			return target;
		}
	}
	return nullptr;
}

Json GoalDestination(const Goal *goal)
{
	return LinkTarget(static_cast<LinkType>(goal->type), goal->dst, _current_company, goal->company == CompanyID::Invalid());
}

Json TownDetail(const Json &parameters)
{
	const Town *town = CheckedHandle<Town, 64000>(parameters, "town_id");
	AdmitWorldText();
	Json result = SnapshotBase();
	result["town_id"] = Handle(town);
	PutDisplayText(result, "name", GetString(STR_TOWN_NAME, town->index));
	result["location"] = TileCoordinates(town->xy);
	result["city"] = town->larger_town;
	result["population"] = town->cache.population;
	result["house_count"] = town->cache.num_houses;
	PutDisplayText(result, "text", town->text.GetDecodedString());
	result["supplied_cargo"] = Json::array();
	for (auto effect : {TownProductionEffect::Passengers, TownProductionEffect::Mail}) {
		for (const CargoSpec *cargo : CargoSpec::town_production_cargoes[effect]) {
			const auto supplied = town->GetCargoSupplied(cargo->Index());
			Json item = {{"cargo_id", to_underlying(cargo->Index())},
				{"produced_last_period", supplied == std::end(town->supplied) ? 0 : supplied->history[LAST_MONTH].production},
				{"transported_last_period", supplied == std::end(town->supplied) ? 0 : supplied->history[LAST_MONTH].transported}};
			PutDisplayText(item, "name", GetString(cargo->name));
			result["supplied_cargo"].push_back(std::move(item));
		}
	}
	result["growth_requirements"] = Json::array();
	for (TownAcceptanceEffect effect : EnumRange(TownAcceptanceEffect::End)) {
		uint32_t goal = town->goal[effect];
		if (goal == 0) continue;
		if (goal == TOWN_GROWTH_WINTER && (TileHeight(town->xy) < LowestSnowLine() || town->cache.population <= 90)) continue;
		if (goal == TOWN_GROWTH_DESERT && (GetTropicZone(town->xy) != TropicZone::Desert || town->cache.population <= 60)) continue;
		const CargoSpec *cargo = FindFirstCargoWithTownAcceptanceEffect(effect);
		if (cargo == nullptr) continue;
		bool original = goal == TOWN_GROWTH_WINTER || goal == TOWN_GROWTH_DESERT;
		uint32_t delivered = town->received[effect].old_act;
		const char *status = delivered >= goal ? "delivered" : "required";
		if (original) {
			status = delivered > 0 ? "delivered" : "required";
			if (goal == TOWN_GROWTH_WINTER && delivered == 0 && TileHeight(town->xy) < GetSnowLine()) status = "required_in_winter";
		}
		Json item = {{"cargo_id", to_underlying(cargo->Index())}, {"kind", goal == TOWN_GROWTH_WINTER ? "winter" : goal == TOWN_GROWTH_DESERT ? "desert" : "minimum"},
			{"status", status}, {"delivered_last_period", original ? Json(nullptr) : Json(delivered)}, {"required_amount", original ? Json(nullptr) : Json(goal)}};
		PutDisplayText(item, "name", GetString(cargo->name));
		result["growth_requirements"].push_back(std::move(item));
	}
	bool growing = town->flags.Test(TownFlag::IsGrowing);
	result["growth"] = {{"growing", growing}, {"interval_days", growing ? Json(RoundDivSU(town->growth_rate + 1, Ticks::DAY_TICKS)) : Json(nullptr)},
		{"funded", growing ? Json(town->fund_buildings_months != 0) : Json(nullptr)}};
	result["airport_noise"] = _settings_game.economy.station_noise_level ? Json{{"current", town->noise_reached}, {"limit", town->MaxTownNoise()}} : Json(nullptr);
	return BoundedWorld(std::move(result));
}

std::optional<Json> SubsidyEndpoint(const Source &source)
{
	Json endpoint;
	switch (source.type) {
		case SourceType::Town: {
			const Town *town = Town::GetIfValid(source.id);
			if (town == nullptr) return std::nullopt;
			endpoint = {{"kind", "town"}, {"entity", Handle(town)}, {"location", TileCoordinates(town->xy)}};
			PutDisplayText(endpoint, "name", GetString(STR_TOWN_NAME, town->index));
			return endpoint;
		}
		case SourceType::Industry: {
			const Industry *industry = Industry::GetIfValid(source.id);
			if (industry == nullptr) return std::nullopt;
			endpoint = {{"kind", "industry"}, {"entity", Handle(industry)}, {"location", TileCoordinates(industry->location.tile)}};
			PutDisplayText(endpoint, "name", GetString(STR_INDUSTRY_NAME, industry->index));
			return endpoint;
		}
		default: return std::nullopt;
	}
}

bool ValidSubsidyEndpoint(const Source &source)
{
	switch (source.type) {
		case SourceType::Town: return Town::GetIfValid(source.id) != nullptr;
		case SourceType::Industry: return Industry::GetIfValid(source.id) != nullptr;
		default: return false;
	}
}

Json Subsidies(const Json &parameters)
{
	Parameters p(parameters);
	p.Int("after_id", -1, 255, -1);
	p.Int("limit", 1, 64, 32);
	AdmitWorldText();
	Json result = SnapshotBase();
	return WorldPage<Subsidy, 256>(p, std::move(result), [](const Subsidy *subsidy) {
		return ValidWorldCargo(subsidy->cargo_type) && ValidSubsidyEndpoint(subsidy->src) && ValidSubsidyEndpoint(subsidy->dst);
	}, [](const Subsidy *subsidy) {
		const CargoSpec *cargo = CargoSpec::Get(subsidy->cargo_type);
		Json item = {{"subsidy_id", Handle(subsidy)}, {"status", subsidy->IsAwarded() ? "awarded" : "offered"},
			{"cargo_id", to_underlying(subsidy->cargo_type)}, {"source", *SubsidyEndpoint(subsidy->src)}, {"destination", *SubsidyEndpoint(subsidy->dst)},
			{"awarded_company", nullptr}, {"remaining_periods", subsidy->remaining},
			{"period_unit", TimerGameEconomy::UsingWallclockUnits() ? "minute" : "month"}, {"expires_economy_date_day", nullptr}};
		PutDisplayText(item, "cargo_name", GetString(cargo->name));
		if (subsidy->IsAwarded()) {
			const Company *company = Company::GetIfValid(subsidy->awarded);
			if (company != nullptr) item["awarded_company"] = Handle(company);
		}
		if (!TimerGameEconomy::UsingWallclockUnits()) {
			auto ymd = TimerGameEconomy::ConvertDateToYMD(TimerGameEconomy::date);
			item["expires_economy_date_day"] = TimerGameEconomy::date.base() - ymd.day + subsidy->remaining * 32;
		}
		return item;
	});
}

Json PerformanceLeague()
{
	AdmitWorldText();
	static constexpr std::array<StringID, 16> titles = {
		STR_COMPANY_LEAGUE_PERFORMANCE_TITLE_ENGINEER, STR_COMPANY_LEAGUE_PERFORMANCE_TITLE_ENGINEER,
		STR_COMPANY_LEAGUE_PERFORMANCE_TITLE_TRAFFIC_MANAGER, STR_COMPANY_LEAGUE_PERFORMANCE_TITLE_TRAFFIC_MANAGER,
		STR_COMPANY_LEAGUE_PERFORMANCE_TITLE_TRANSPORT_COORDINATOR, STR_COMPANY_LEAGUE_PERFORMANCE_TITLE_TRANSPORT_COORDINATOR,
		STR_COMPANY_LEAGUE_PERFORMANCE_TITLE_ROUTE_SUPERVISOR, STR_COMPANY_LEAGUE_PERFORMANCE_TITLE_ROUTE_SUPERVISOR,
		STR_COMPANY_LEAGUE_PERFORMANCE_TITLE_DIRECTOR, STR_COMPANY_LEAGUE_PERFORMANCE_TITLE_DIRECTOR,
		STR_COMPANY_LEAGUE_PERFORMANCE_TITLE_CHIEF_EXECUTIVE, STR_COMPANY_LEAGUE_PERFORMANCE_TITLE_CHIEF_EXECUTIVE,
		STR_COMPANY_LEAGUE_PERFORMANCE_TITLE_CHAIRMAN, STR_COMPANY_LEAGUE_PERFORMANCE_TITLE_CHAIRMAN,
		STR_COMPANY_LEAGUE_PERFORMANCE_TITLE_PRESIDENT, STR_COMPANY_LEAGUE_PERFORMANCE_TITLE_TYCOON,
	};
	std::vector<const Company *> companies;
	for (const Company *company : Company::Iterate()) companies.push_back(company);
	std::stable_sort(companies.begin(), companies.end(), [](const Company *a, const Company *b) {
		return a->old_economy[0].performance_history > b->old_economy[0].performance_history;
	});
	Json result = SnapshotBase();
	result["rows"] = Json::array();
	for (size_t i = 0; i < companies.size(); ++i) {
		const Company *company = companies[i];
		Json row = {{"rank", i + 1}, {"company", Handle(company)}};
		PutDisplayText(row, "name", GetString(STR_COMPANY_NAME, company->index));
		PutDisplayText(row, "title", GetString(titles[std::min<uint>(company->old_economy[0].performance_history, 1000) >> 6]));
		result["rows"].push_back(std::move(row));
	}
	return BoundedWorld(std::move(result));
}

Json LeagueTables(const Json &parameters)
{
	Parameters p(parameters);
	p.Int("after_id", -1, 254, -1);
	p.Int("limit", 1, 64, 32);
	AdmitWorldText();
	return WorldPage<LeagueTable, 255>(p, SnapshotBase(), [](const LeagueTable *) { return true; }, [](const LeagueTable *table) {
		Json item = {{"table_id", Handle(table)}};
		PutDisplayText(item, "title", table->title.GetDecodedString());
		return item;
	});
}

Json LeagueTableEntries(const Json &parameters)
{
	const LeagueTable *table = CheckedHandle<LeagueTable, 255>(parameters, "table_id");
	Parameters p(parameters);
	p.Int("after_id", -1, 63999, -1);
	p.Int("limit", 1, 64, 32);
	AdmitWorldText();
	Json result = SnapshotBase();
	result["table_id"] = Handle(table);
	PutDisplayText(result, "title", table->title.GetDecodedString());
	PutDisplayText(result, "header", table->header.GetDecodedString());
	PutDisplayText(result, "footer", table->footer.GetDecodedString());
	return WorldPage<LeagueTableElement, 64000>(p, std::move(result), [table](const LeagueTableElement *entry) { return entry->table == table->index; }, [](const LeagueTableElement *entry) {
		Json item = {{"entry_id", Handle(entry)}, {"company", nullptr}, {"link", LinkTarget(entry->link.type, entry->link.target, _current_company)}};
		if (const Company *company = Company::GetIfValid(entry->company)) item["company"] = Handle(company);
		PutDisplayText(item, "text", entry->text.GetDecodedString());
		PutDisplayText(item, "score", entry->score.GetDecodedString());
		return item;
	});
}

Json Goals(const Json &parameters)
{
	Parameters p(parameters);
	std::string scope = p.Text("scope", "company", 7);
	if (scope != "global" && scope != "company") throw InvalidParameters("Invalid goal scope");
	p.Int("after_id", -1, 63999, -1);
	p.Int("limit", 1, 64, 32);
	AdmitWorldText();
	CompanyID company = scope == "global" ? CompanyID::Invalid() : _current_company;
	Json result = SnapshotBase();
	result["scope"] = scope;
	return WorldPage<Goal, 64000>(p, std::move(result), [company](const Goal *goal) { return goal->company == company; }, [](const Goal *goal) {
		Json item = {{"goal_id", Handle(goal)}, {"completed", nullptr}, {"destination", GoalDestination(goal)}};
		PutDisplayText(item, "text", goal->text.GetDecodedString());
		if (!goal->progress.empty()) {
			PutDisplayText(item, "progress", goal->progress.GetDecodedString());
			item["completed"] = goal->completed;
		} else {
			item["progress"] = "";
			item["progress_truncated"] = false;
		}
		return item;
	});
}

/** A finite reader for the display strings previewed by this family. */
Json DisplayText(const Json &parameters)
{
	if (!parameters.is_object()) throw InvalidParameters("Invalid display text parameters");
	for (auto it = parameters.begin(); it != parameters.end(); ++it) {
		std::string_view key = it.key();
		if (key != "kind" && key != "target" && key != "cargo_id" && key != "vehicle_id" && key != "direction" && key != "slot" && key != "subtype" &&
				key != "after_byte" && key != "limit" && key != "expected_content_version") {
			throw InvalidParameters("Unknown display text parameter");
		}
	}
	Parameters p(parameters);
	const std::string kind = p.Text("kind", "", 32);
	if (kind.empty()) throw InvalidParameters("Missing display text kind");
	const uint64_t after = UnsignedOffset(parameters);
	const size_t limit = static_cast<size_t>(p.Int("limit", 1, DISPLAY_TEXT_BYTES, DISPLAY_TEXT_BYTES));
	std::string expected = p.Text("expected_content_version", "", 64);
	if (after > 0 && expected.empty()) throw InvalidParameters("Missing text version");
	if (parameters.contains("expected_content_version")) {
		if (expected.size() != 64 || !std::all_of(expected.begin(), expected.end(), [](char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); })) {
			throw InvalidParameters("Invalid text version");
		}
	}

	Json reference;
	std::function<std::string()> render;
	bool gate_in_renderer = false;
	if (kind == "cargo_name") {
		CheckDisplayTextKeys(parameters, {"cargo_id"});
		auto id = static_cast<CargoType>(p.Int("cargo_id", 0, NUM_CARGO - 1));
		if (!ValidWorldCargo(id)) throw InvalidParameters("Cargo unavailable");
		reference = {{"cargo_id", to_underlying(id)}};
		render = [id]() { return GetString(CargoSpec::Get(id)->name); };
	} else if (kind == "vehicle_refit_label") {
		CheckDisplayTextKeys(parameters, {"target", "vehicle_id", "cargo_id", "subtype"});
		if (!parameters.contains("target") || !parameters.contains("vehicle_id")) throw InvalidParameters("Vehicle refit label requires two handles");
		auto cargo = static_cast<uint8_t>(p.Int("cargo_id", 0, NUM_CARGO - 1));
		auto subtype = static_cast<uint8_t>(p.Int("subtype", 0, 254));
		const Json part_handle = parameters.at("target"), vehicle_handle = parameters.at("vehicle_id");
		reference = {{"target", part_handle}, {"vehicle_id", vehicle_handle}, {"cargo_id", cargo}, {"subtype", subtype}};
		/* Transport validates both handles, ownership, membership and refit before
		 * consuming the shared gate, then probes only the selected part's label. */
		render = [vehicle_handle, part_handle, cargo, subtype]() {
			return GetString(ResolveVehicleRefitLabelString(vehicle_handle, part_handle, cargo, subtype));
		};
		gate_in_renderer = true;
	} else if (kind == "industry_cargo_line" || kind == "industry_extra_text" || kind == "industry_name" || kind == "industry_custom_text") {
		const bool cargo_line = kind == "industry_cargo_line";
		CheckDisplayTextKeys(parameters, cargo_line ? std::initializer_list<std::string_view>{"target", "direction", "slot", "cargo_id"} : std::initializer_list<std::string_view>{"target"});
		const Industry *industry = CheckedDisplayIndustry(parameters);
		reference = {{"target", Handle(industry)}};
		IndustryDisplayTextKind source = IndustryDisplayTextKind::Name;
		uint8_t slot = 0;
		if (cargo_line) {
			const std::string direction = p.Text("direction", "", 8);
			slot = static_cast<uint8_t>(p.Int("slot", 0, 15));
			const CargoType cargo = static_cast<CargoType>(p.Int("cargo_id", 0, NUM_CARGO - 1));
			if (!ValidWorldCargo(cargo)) throw InvalidParameters("Industry cargo unavailable");
			if (direction == "accepted") {
				if (industry->GetAccepted(slot).cargo != cargo) throw InvalidParameters("Industry cargo slot changed");
				source = IndustryDisplayTextKind::AcceptedLine;
			} else if (direction == "produced") {
				if (industry->GetProduced(slot).cargo != cargo) throw InvalidParameters("Industry cargo slot changed");
				source = IndustryDisplayTextKind::ProducedLine;
			} else {
				throw InvalidParameters("Invalid industry cargo direction");
			}
			reference["direction"] = direction;
			reference["slot"] = slot;
			reference["cargo_id"] = to_underlying(cargo);
		} else if (kind == "industry_extra_text") {
			source = IndustryDisplayTextKind::ExtraText;
		} else if (kind == "industry_custom_text") {
			source = IndustryDisplayTextKind::CustomText;
		}
		render = [industry, source, slot]() { return RenderIndustryDisplayText(*industry, source, slot); };
	} else {
		CheckDisplayTextKeys(parameters, {"target"});
		if (!parameters.contains("target")) throw InvalidParameters("Display text target is missing");
		if (kind == "news_custom_text") {
			const EncodedString *text = &CustomNewsText(parameters.at("target"));
			reference = {{"target", parameters.at("target")}};
			render = [text]() { return text->GetDecodedString(); };
		} else if (kind == "town_text") {
			const Town *town = CheckedHandle<Town, 64000>(parameters, "target");
			reference = {{"target", Handle(town)}};
			render = [town]() { return town->text.GetDecodedString(); };
		} else if (kind == "subsidy_cargo_name" || kind == "subsidy_source_name" || kind == "subsidy_destination_name") {
			const Subsidy *subsidy = CheckedHandle<Subsidy, 256>(parameters, "target");
			if (!ValidWorldCargo(subsidy->cargo_type) || !ValidSubsidyEndpoint(subsidy->src) || !ValidSubsidyEndpoint(subsidy->dst)) {
				throw InvalidParameters("Subsidy unavailable");
			}
			reference = {{"target", Handle(subsidy)}};
			if (kind == "subsidy_cargo_name") {
				render = [subsidy]() { return GetString(CargoSpec::Get(subsidy->cargo_type)->name); };
			} else {
				Source endpoint = kind == "subsidy_source_name" ? subsidy->src : subsidy->dst;
				render = [endpoint]() { return GetString(endpoint.GetFormat(), endpoint.id); };
			}
		} else if (kind == "league_table_title" || kind == "league_table_header" || kind == "league_table_footer") {
			const LeagueTable *table = CheckedHandle<LeagueTable, 255>(parameters, "target");
			reference = {{"target", Handle(table)}};
			if (kind == "league_table_title") render = [table]() { return table->title.GetDecodedString(); };
			else if (kind == "league_table_header") render = [table]() { return table->header.GetDecodedString(); };
			else render = [table]() { return table->footer.GetDecodedString(); };
		} else if (kind == "league_entry_text" || kind == "league_entry_score") {
			const LeagueTableElement *entry = CheckedHandle<LeagueTableElement, 64000>(parameters, "target");
			if (LeagueTable::GetIfValid(entry->table) == nullptr) throw InvalidParameters("League entry unavailable");
			reference = {{"target", Handle(entry)}};
			if (kind == "league_entry_text") render = [entry]() { return entry->text.GetDecodedString(); };
			else render = [entry]() { return entry->score.GetDecodedString(); };
		} else if (kind == "goal_text" || kind == "goal_progress") {
			const Goal *goal = CheckedHandle<Goal, 64000>(parameters, "target");
			if (goal->company != CompanyID::Invalid() && goal->company != _current_company) throw InvalidParameters("Goal unavailable");
			reference = {{"target", Handle(goal)}};
			if (kind == "goal_text") render = [goal]() { return goal->text.GetDecodedString(); };
			else render = [goal]() { return goal->progress.GetDecodedString(); };
		} else {
			throw InvalidParameters("Unknown display text kind");
		}
	}

	if (!gate_in_renderer) AdmitWorldText();
	std::string full = PlainText(render());
	std::string version = TextVersion(full);
	if (!expected.empty() && expected != version) throw InvalidParameters("Display text changed");
	if (after > full.size() || (after < full.size() && (static_cast<uint8_t>(full[static_cast<size_t>(after)]) & 0xC0) == 0x80)) {
		throw InvalidParameters("Invalid text offset");
	}
	size_t start = static_cast<size_t>(after);
	size_t end = std::min(full.size(), start + limit);
	while (end < full.size() && end > start && (static_cast<uint8_t>(full[end]) & 0xC0) == 0x80) --end;
	if (end == start && start < full.size()) throw InvalidParameters("Text limit splits a character");
	Json result = SnapshotBase();
	result["kind"] = kind;
	result.update(reference);
	result["text"] = full.substr(start, end - start);
	result["content_version"] = std::move(version);
	result["total_bytes"] = full.size();
	result["next_after_byte"] = end < full.size() ? Json(end) : Json(nullptr);
	return BoundedWorld(std::move(result));
}

} // namespace

/** Shared admission for world text and the native vehicle refit probe. */
void AdmitWorldTextStart()
{
	AdmitWorldText();
}

std::optional<Json> QueryWorldDetailsState(const std::string &query, const Json &parameters)
{
	if (query == "town.detail") return TownDetail(parameters);
	if (query == "subsidies") return Subsidies(parameters);
	if (query == "league.performance") return PerformanceLeague();
	if (query == "league.tables") return LeagueTables(parameters);
	if (query == "league.table.entries") return LeagueTableEntries(parameters);
	if (query == "goals") return Goals(parameters);
	if (query == "display.text") return DisplayText(parameters);
	return std::nullopt;
}

} // namespace Integration::Detail
