/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file test_integration_custom_news.cpp Recipient-local native custom-news history and lazy text. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions_internal.h"
#include "../integration/actions_story.h"
#include "../integration/bridge_hooks.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../core/endian_func.hpp"
#include "../core/pool_func.hpp"
#include "../core/random_func.hpp"
#include "../engine_base.h"
#include "../language.h"
#include "../network/network.h"
#include "../newgrf.h"
#include "../newgrf_engine.h"
#include "../newgrf_spritegroup.h"
#include "../newgrf_text.h"
#include "../news_cmd.h"
#include "../news_func.h"
#include "../news_gui.h"
#include "../openttd.h"
#include "../strings_func.h"
#include "../timer/timer_game_tick.h"
#include "../town.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <thread>

#include "table/strings.h"
#include "../safeguards.h"

namespace {

using namespace Integration;
using namespace Integration::Detail;

class NewsEnvironment {
	const char *name;
	std::optional<std::string> previous;

	static int Set(const char *name, const std::optional<std::string> &value)
	{
#ifdef _WIN32
		return _putenv_s(name, value.has_value() ? value->c_str() : "");
#else
		return value.has_value() ? setenv(name, value->c_str(), 1) : unsetenv(name);
#endif
	}

public:
	explicit NewsEnvironment(const char *name) : name(name)
	{
		if (const char *value = std::getenv(name); value != nullptr) this->previous = value;
	}
	~NewsEnvironment() { Set(this->name, this->previous); }
	void Value(const std::optional<std::string> &value) { REQUIRE(Set(this->name, value) == 0); }
};

void EnsureNewsLanguage()
{
	if (_current_language != nullptr) return;
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

EncodedString RawNews(std::string_view text) { return GetEncodedString(STR_JUST_RAW_STRING, text); }

EncodedString EncodedNewsOfSize(size_t bytes)
{
	const size_t overhead = RawNews("").GetEncodedSize();
	REQUIRE(bytes >= overhead);
	EncodedString text = RawNews(std::string(bytes - overhead, 'N'));
	REQUIRE(text.GetEncodedSize() == bytes);
	return text;
}

void WaitForNewsText() { std::this_thread::sleep_for(std::chrono::milliseconds(1050)); }

struct CustomNewsWorld {
	bool networking = _networking;
	bool server = _network_server;
	CompanyID previous_company = _current_company;
	CompanyID previous_local = _local_company;
	GameMode previous_mode = _game_mode;
	Randomizer random = _random;
	Randomizer interactive_random = _interactive_random;
	NewsEnvironment socket{"OPENTTD_INTEGRATION_SOCKET"};
	NewsEnvironment role{"OPENTTD_INTEGRATION_ROLE"};
	std::vector<Company *> companies;

	CustomNewsWorld()
	{
		EnsureNewsLanguage();
		this->socket.Value("/openttd-custom-news-regression/no-listener.sock");
		this->role.Value(std::nullopt);
		_networking = true;
		_network_server = true;
		_local_company = COMPANY_SPECTATOR;
		_current_company = OWNER_DEITY;
		_game_mode = GameMode::Normal;
		if (Map::Size() == 0) Map::Allocate(64, 64);
		/* These tests run in the isolated native test process. Clear native
		 * local news too, so broadcasts cannot retain fixture object references. */
		InitNewsItemStructs();
		ResetStoryPresentations();
		this->NewCompany();
		this->NewCompany();
	}

	~CustomNewsWorld()
	{
		InitNewsItemStructs();
		ResetStoryPresentations();
		for (Company *company : this->companies) delete company;
		_networking = this->networking;
		_network_server = this->server;
		_current_company = this->previous_company;
		_local_company = this->previous_local;
		_game_mode = this->previous_mode;
		_random = this->random;
		_interactive_random = this->interactive_random;
	}

	Company *NewCompany()
	{
		REQUIRE(Company::CanAllocateItem());
		Company *company = Company::Create();
		this->companies.push_back(company);
		return company;
	}

	CommandCost Present(const EncodedString &text, CompanyID company, NewsType type = NewsType::General, NewsReference reference = {}, DoCommandFlags flags = DoCommandFlag::Execute) const
	{
		_current_company = OWNER_DEITY;
		return CmdCustomNewsItem(flags, type, company, reference, text);
	}

	Json Page(Company *company, Json parameters = Json::object()) const
	{
		_current_company = company->index;
		auto result = QueryStoryState("news.custom", parameters);
		REQUIRE(result.has_value());
		REQUIRE(result->dump().size() <= 40960);
		return std::move(*result);
	}

	Json Text(Company *company, const Json &target, Json options = Json::object()) const
	{
		_current_company = company->index;
		options["kind"] = "news_custom_text";
		options["target"] = target;
		return QueryState("display.text", options);
	}
};

/** Runs through the real engine-name resolver and its owner variable. The
 * counter measures the production formatting path, not a mocked news decoder. */
struct OwnerNameGroup : SpecializedSpriteGroup<OwnerNameGroup> {
	CompanyID first;
	mutable std::array<unsigned, MAX_COMPANIES> calls{};

	OwnerNameGroup(SpriteGroupID index, CompanyID first) : SpecializedSpriteGroup(index), first(first) {}

	unsigned Count() const
	{
		unsigned total = 0;
		for (unsigned count : this->calls) total += count;
		return total;
	}

	ResolverResult Resolve(ResolverObject &object) const override
	{
		bool available = true;
		const uint32_t owner = object.GetScope()->GetVariable(0x43, 0, available) & 0xFF;
		CHECK(available);
		REQUIRE(owner < MAX_COMPANIES);
		++this->calls[owner];
		return CallbackResult{static_cast<uint16_t>(owner == this->first.base() ? 0 : 1)};
	}
};

void CheckNewsMetadata(const Json &item)
{
	CHECK(item.size() == 5);
	for (const char *key : {"news_id", "issued_tick", "calendar_date", "news_type", "reference"}) CHECK(item.contains(key));
	for (const char *key : {"text", "text_truncated", "text_bytes", "headline", "encoded", "body"}) CHECK_FALSE(item.contains(key));
	CHECK(item.at("calendar_date").at("month").get<unsigned>() >= 1);
	CHECK(item.at("calendar_date").at("month").get<unsigned>() <= 12);
	CHECK(item.at("calendar_date").at("day").get<unsigned>() >= 1);
	CHECK(item.at("calendar_date").at("day").get<unsigned>() <= 31);
}

} // namespace

TEST_CASE("Custom news observes only validated authority Execute presentations before bridge startup", "[integration][custom-news][startup]")
{
	CustomNewsWorld world;
	Company *own = world.companies[0];
	const EncodedString text = RawNews("startup news");
	REQUIRE(TracksPlayerPresentations());
	REQUIRE_FALSE(Enabled());
	REQUIRE(world.Page(own).at("latest").at("id") == 0);
	CHECK(world.Present(text, own->index, NewsType::General, {}, {}).Succeeded());
	CHECK(world.Page(own).at("latest").at("id") == 0);

	SECTION("Native validation failures are never retained")
	{
		_current_company = own->index;
		CHECK(CmdCustomNewsItem(DoCommandFlag::Execute, NewsType::General, own->index, {}, text).Failed());
		CHECK(world.Present(text, CompanyID{MAX_COMPANIES}).Failed());
		CHECK(world.Present(text, own->index, NewsType::End).Failed());
		CHECK(world.Present(EncodedString{}, own->index).Failed());
		CHECK(world.Present(text, own->index, NewsType::General, VehicleID::Invalid()).Failed());
		CHECK(world.Present(text, own->index, NewsType::General, TileIndex{Map::Size()}).Failed());
	}
	SECTION("Disabled integration preserves the accepted native command")
	{
		world.socket.Value(std::nullopt);
		CHECK(world.Present(text, own->index).Succeeded());
		world.socket.Value("/openttd-custom-news-regression/no-listener.sock");
	}
	SECTION("An ordinary native client cannot fill authority history")
	{
		_network_server = false;
		CHECK(world.Present(text, own->index).Succeeded());
		_network_server = true;
	}
	SECTION("A non-network game cannot fill authority history")
	{
		_networking = false;
		CHECK(world.Present(text, own->index).Succeeded());
		_networking = true;
	}
	SECTION("A renderer role cannot fill authority history")
	{
		world.role.Value("renderer");
		CHECK(world.Present(text, own->index).Succeeded());
		world.role.Value(std::nullopt);
	}
	CHECK(world.Page(own).at("latest").at("id") == 0);
	REQUIRE(world.Present(text, own->index).Succeeded());
	CHECK(_current_company == OWNER_DEITY);
	CHECK(_local_company == COMPANY_SPECTATOR);
	const Json shown = world.Page(own);
	REQUIRE(shown.at("items").size() == 1);
	CHECK(shown.at("history_scope") == "current_engine_instance");
	CHECK(shown.at("history_start").at("id") == 0);
	CHECK(shown.at("latest").at("id") == 1);
	CHECK(shown.at("next") == shown.at("latest"));
	CHECK_FALSE(shown.at("gap").get<bool>());
	CHECK(shown.at("complete").get<bool>());
	CheckNewsMetadata(shown.at("items").at(0));
	CHECK(shown.at("items").at(0).at("issued_tick") == TimerGameTick::counter);
	CHECK(shown.at("items").at(0).at("reference").is_null());
	CHECK(CustomNewsText(shown.at("items").at(0).at("news_id")) == text);
	CHECK_FALSE(Enabled());
}

TEST_CASE("Custom news keeps private sequences and excludes future and foreign recipient cursors", "[integration][custom-news][privacy]")
{
	CustomNewsWorld world;
	Company *own = world.companies[0], *other = world.companies[1];
	REQUIRE(world.Present(RawNews("A private"), own->index).Succeeded());
	const Json own_first = world.Page(own).at("items").at(0).at("news_id");
	CHECK(world.Page(other).at("latest").at("id") == 0);
	REQUIRE(world.Present(RawNews("broadcast"), INVALID_OWNER).Succeeded());
	REQUIRE(world.Present(RawNews("B private"), other->index).Succeeded());
	const Json a = world.Page(own), b = world.Page(other);
	REQUIRE(a.at("items").size() == 2);
	REQUIRE(b.at("items").size() == 2);
	CHECK(a.at("latest").at("id") == 2);
	CHECK(b.at("latest").at("id") == 2);
	CHECK(a.at("items").at(1).at("news_id").at("id") == 2);
	CHECK(b.at("items").at(0).at("news_id").at("id") == 1);
	_current_company = other->index;
	CHECK_THROWS_AS(QueryStoryState("news.custom", {{"after", own_first}}), InvalidParameters);
	CHECK_THROWS_AS(CustomNewsText(own_first), InvalidParameters);
	_current_company = own->index;
	Json wrong_boot = own_first;
	wrong_boot["engine_instance_id"] = "another-authority-boot";
	CHECK_THROWS_AS(QueryStoryState("news.custom", {{"after", wrong_boot}}), InvalidParameters);
	CHECK_THROWS_AS(CustomNewsText(wrong_boot), InvalidParameters);
	Json future = own_first;
	future["id"] = 3;
	CHECK_THROWS_AS(QueryStoryState("news.custom", {{"after", future}}), InvalidParameters);
	CHECK_THROWS_AS(CustomNewsText(future), InvalidParameters);
	Json zero = own_first;
	zero["id"] = 0;
	CHECK_THROWS_AS(CustomNewsText(zero), InvalidParameters);
	Json malformed = own_first;
	malformed["id"] = -1;
	CHECK_THROWS_AS(QueryStoryState("news.custom", {{"after", malformed}}), InvalidParameters);
	malformed["id"] = 1.5;
	CHECK_THROWS_AS(QueryStoryState("news.custom", {{"after", malformed}}), InvalidParameters);
	malformed = own_first;
	malformed["id"] = INT64_MAX;
	CHECK_THROWS_AS(QueryStoryState("news.custom", {{"after", malformed}}), InvalidParameters);
	malformed["id"] = static_cast<uint64_t>(INT64_MAX) + 1;
	CHECK_THROWS_AS(QueryStoryState("news.custom", {{"after", malformed}}), InvalidParameters);
	CHECK(QueryState("news.custom", {{"after", malformed}}).contains("error"));
	malformed = own_first;
	malformed["generation"] = 0;
	CHECK_THROWS_AS(CustomNewsText(malformed), InvalidParameters);
	malformed = own_first;
	malformed["extra"] = true;
	CHECK_THROWS_AS(CustomNewsText(malformed), InvalidParameters);
	CHECK(QueryState("news.custom", {{"company_id", other->index.base()}}).contains("error"));
	CHECK(QueryState("news.custom", {{"limit", 0}}).contains("error"));
	CHECK(QueryState("news.custom", {{"limit", 33}}).contains("error"));
	CHECK(QueryState("news.custom", {{"after", own_first}, {"extra", true}}).contains("error"));
	Company *late = world.NewCompany();
	CHECK(world.Page(late).at("items").empty());
	CHECK(world.Page(late).at("latest").at("id") == 0);
	const Json empty = world.Page(own, {{"after", a.at("latest")}});
	CHECK(empty.at("items").empty());
	CHECK(empty.at("next") == a.at("latest"));
	CHECK(empty.at("complete").get<bool>());
}

TEST_CASE("Custom news exposes finite native types and its own full-width cursor schema", "[integration][custom-news][capabilities]")
{
	CustomNewsWorld world;
	Company *own = world.companies[0];
	static constexpr std::array<std::string_view, 16> names = {
		"arrival_company", "arrival_other", "accident", "accident_other", "company_info", "industry_open", "industry_close", "economy",
		"industry_company", "industry_other", "industry_nobody", "advice", "new_vehicles", "acceptance", "subsidies", "general",
	};
	static_assert(names.size() == to_underlying(NewsType::End));
	for (size_t type = 0; type < names.size(); ++type) REQUIRE(world.Present(RawNews("finite category"), own->index, static_cast<NewsType>(type)).Succeeded());
	const Json page = world.Page(own);
	REQUIRE(page.at("items").size() == names.size());
	for (size_t type = 0; type < names.size(); ++type) CHECK(page.at("items").at(type).at("news_type") == names[type]);
	const Json queries = CapabilityMatrix().at("queries");
	auto find_query = [&](std::string_view name) -> const Json & {
		const auto found = std::ranges::find_if(queries, [&](const Json &query) { return query.at("name") == name; });
		REQUIRE(found != queries.end());
		return *found;
	};
	const Json &news = find_query("news.custom");
	CHECK(news.at("modes") == Json::array({"text"}));
	CHECK(news.at("parameters").at("properties").at("after").at("properties").at("id").at("maximum") == INT64_MAX);
	size_t news_branches = 0;
	for (const Json &branch : find_query("display.text").at("parameters").at("anyOf")) {
		const Json &kinds = branch.at("properties").at("kind").at("enum");
		if (std::ranges::find(kinds, "news_custom_text") == kinds.end()) continue;
		++news_branches;
		CHECK(branch.at("properties").at("target").at("properties").at("id").at("minimum") == 1);
		CHECK(branch.at("properties").at("target").at("properties").at("id").at("maximum") == INT64_MAX);
	}
	CHECK(news_branches == 2); // Initial read and versioned nonzero continuation.
}

TEST_CASE("Custom news broadcast reaches all fifteen live companies without formatting a body", "[integration][custom-news][broadcast]")
{
	CustomNewsWorld world;
	while (Company::CanAllocateItem()) world.NewCompany();
	REQUIRE(Company::GetNumItems() == MAX_COMPANIES);
	std::vector<std::pair<Company *, int64_t>> before;
	for (Company *company : Company::Iterate()) before.emplace_back(company, world.Page(company).at("latest").at("id").get<int64_t>());
	REQUIRE(world.Present(RawNews("all live recipients"), INVALID_OWNER).Succeeded());
	for (const auto &[company, sequence] : before) {
		const Json page = world.Page(company);
		CHECK(page.at("latest").at("id") == sequence + 1);
		REQUIRE_FALSE(page.at("items").empty());
		const Json &item = page.at("items").back();
		CheckNewsMetadata(item);
		CHECK(item.at("news_id").at("generation") == company->GetInstanceGeneration());
		CHECK(item.at("news_id").at("engine_instance_id") == InstanceId());
	}
	/* Native client-local reset is deliberately independent of this history. */
	InitNewsItemStructs();
	for (const auto &[company, sequence] : before) CHECK(world.Page(company).at("latest").at("id") == sequence + 1);
	const EncodedString maximum = EncodedNewsOfSize(65536);
	REQUIRE(world.Present(maximum, INVALID_OWNER).Succeeded());
	size_t copied_body_bytes = 0;
	for (const auto &[company, sequence] : before) {
		const Json page = world.Page(company);
		REQUIRE_FALSE(page.at("items").empty());
		const Json &cursor = page.at("items").back().at("news_id");
		CHECK(cursor.at("id") == sequence + 2);
		const size_t bytes = CustomNewsText(cursor).GetEncodedSize();
		CHECK(bytes == 65536);
		copied_body_bytes += bytes;
	}
	CHECK(copied_body_bytes == MAX_COMPANIES * size_t{65536});
	REQUIRE(world.Present(EncodedNewsOfSize(65537), INVALID_OWNER).Succeeded());
	for (const auto &[company, sequence] : before) {
		const Json page = world.Page(company);
		CHECK(page.at("items").empty());
		CHECK(page.at("latest").at("id") == sequence + 3);
		CHECK(page.at("history_start").at("id") == sequence + 3);
		CHECK(page.at("gap").get<bool>());
	}
	REQUIRE(world.Present(RawNews("broadcast after loss"), INVALID_OWNER).Succeeded());
	for (const auto &[company, sequence] : before) {
		const Json page = world.Page(company);
		REQUIRE(page.at("items").size() == 1);
		CHECK(page.at("items").at(0).at("news_id").at("id") == sequence + 4);
		CHECK(page.at("history_start").at("id") == sequence + 3);
	}
}

TEST_CASE("Custom news resets preserve sequences while a reused company slot gets a fresh namespace", "[integration][custom-news][reset]")
{
	CustomNewsWorld world;
	Company *own = world.companies[0];
	REQUIRE(world.Present(RawNews("before reset"), own->index).Succeeded());
	const Json old = world.Page(own).at("items").at(0).at("news_id");
	ResetStoryPresentations();
	const Json reset = world.Page(own);
	CHECK(reset.at("items").empty());
	CHECK(reset.at("latest").at("id") == 1);
	CHECK(reset.at("history_start").at("id") == 1);
	CHECK(reset.at("next").at("id") == 1);
	CHECK(reset.at("gap").get<bool>());
	CHECK_THROWS_AS(CustomNewsText(old), InvalidParameters);
	REQUIRE(world.Present(RawNews("after reset"), own->index).Succeeded());
	CHECK(world.Page(own).at("items").at(0).at("news_id").at("id") == 2);
	const CompanyID slot = own->index;
	delete own;
	world.companies[0] = Company::CreateAtIndex(slot);
	own = world.companies[0];
	REQUIRE(own->GetInstanceGeneration() != old.at("generation").get<uint64_t>());
	const Json fresh = world.Page(own);
	CHECK(fresh.at("items").empty());
	CHECK(fresh.at("latest").at("id") == 0);
	CHECK_FALSE(fresh.at("gap").get<bool>());
	CHECK_THROWS_AS(QueryStoryState("news.custom", {{"after", old}}), InvalidParameters);
	CHECK_THROWS_AS(CustomNewsText(old), InvalidParameters);
	REQUIRE(world.Present(RawNews("new occupant"), own->index).Succeeded());
	CHECK(world.Page(own).at("items").at(0).at("news_id").at("id") == 1);
}

TEST_CASE("Custom news count eviction is an oldest-first visible history gap", "[integration][custom-news][retention]")
{
	CustomNewsWorld world;
	Company *own = world.companies[0];
	const EncodedString text = RawNews("small ordinary news");
	REQUIRE(world.Present(text, own->index).Succeeded());
	const Json evicted = world.Page(own).at("items").at(0).at("news_id");
	for (unsigned i = 1; i < 1025; ++i) REQUIRE(world.Present(text, own->index).Succeeded());
	Json page = world.Page(own);
	CHECK(page.at("history_start").at("id") == 1);
	CHECK(page.at("latest").at("id") == 1025);
	CHECK(page.at("gap").get<bool>());
	REQUIRE(page.at("items").size() == 32);
	CHECK(page.at("items").at(0).at("news_id").at("id") == 2);
	CHECK(page.at("next").at("id") == 33);
	CHECK_FALSE(page.at("complete").get<bool>());
	CHECK_THROWS_AS(CustomNewsText(evicted), InvalidParameters);
	int64_t expected = 2;
	size_t retained = 0;
	for (;;) {
		for (const auto &item : page.at("items")) {
			CHECK(item.at("news_id").at("id") == expected++);
			++retained;
		}
		if (page.at("complete").get<bool>()) break;
		page = world.Page(own, {{"after", page.at("next")}, {"limit", 32}});
		CHECK_FALSE(page.at("gap").get<bool>());
	}
	CHECK(retained == 1024);
	CHECK(expected == 1026);
	CHECK(page.at("next") == page.at("latest"));
	CHECK(world.Page(world.companies[1]).at("latest").at("id") == 0);
}

TEST_CASE("Custom news bounds encoded bytes before copying and resumes after an oversized loss", "[integration][custom-news][retention]")
{
	CustomNewsWorld world;
	Company *own = world.companies[0];
	SECTION("A 4 MiB byte budget evicts before the row count limit")
	{
		const EncodedString text = EncodedNewsOfSize(65536);
		for (unsigned i = 0; i < 65; ++i) REQUIRE(world.Present(text, own->index).Succeeded());
		const Json page = world.Page(own);
		CHECK(page.at("history_start").at("id") == 1);
		CHECK(page.at("latest").at("id") == 65);
		CHECK(page.at("items").at(0).at("news_id").at("id") == 2);
		CHECK(page.at("gap").get<bool>());
		Json cursor = page.at("history_start");
		size_t retained_bytes = 0, retained_rows = 0;
		for (;;) {
			const Json next = world.Page(own, {{"after", cursor}});
			for (const auto &item : next.at("items")) {
				retained_bytes += CustomNewsText(item.at("news_id")).GetEncodedSize();
				++retained_rows;
			}
			if (next.at("complete").get<bool>()) break;
			cursor = next.at("next");
		}
		CHECK(retained_rows == 64);
		CHECK(retained_bytes == 4 * 1024 * 1024);
	}
	SECTION("Below and at the item cap retain; above it advances the loss floor")
	{
		REQUIRE(world.Present(EncodedNewsOfSize(65535), own->index).Succeeded());
		REQUIRE(world.Present(EncodedNewsOfSize(65536), own->index).Succeeded());
		const Json before = world.Page(own);
		REQUIRE(before.at("items").size() == 2);
		const Json old = before.at("items").at(0).at("news_id");
		const auto cost = world.Present(EncodedNewsOfSize(65537), own->index);
		REQUIRE(cost.Succeeded()); // Observer retention never rejects native news.
		CHECK(cost.GetCost() == 0);
		const Json lost = world.Page(own);
		CHECK(lost.at("items").empty());
		CHECK(lost.at("latest").at("id") == 3);
		CHECK(lost.at("history_start").at("id") == 3);
		CHECK(lost.at("next").at("id") == 3);
		CHECK(lost.at("gap").get<bool>());
		CHECK_THROWS_AS(CustomNewsText(old), InvalidParameters);
		REQUIRE(world.Present(RawNews("retention resumes"), own->index).Succeeded());
		const Json resumed = world.Page(own);
		CHECK(resumed.at("items").at(0).at("news_id").at("id") == 4);
		CHECK(resumed.at("history_start").at("id") == 3);
		CHECK(resumed.at("gap").get<bool>());
	}
}

TEST_CASE("Custom news lazy text chunks obey the existing version and shared admission contract", "[integration][custom-news][display-text]")
{
	CustomNewsWorld world;
	Company *own = world.companies[0];
	std::string text(2047, 'A');
	text += "\xE2\x82\xAC";
	text.append(300, 'B');
	REQUIRE(world.Present(RawNews(text), own->index).Succeeded());
	const Json target = world.Page(own).at("items").at(0).at("news_id");
	WaitForNewsText();
	const Json first = world.Text(own, target);
	REQUIRE_FALSE(first.contains("error"));
	CHECK(first.at("text") == std::string(2047, 'A'));
	CHECK(first.at("next_after_byte") == 2047);
	CHECK(first.at("total_bytes") == text.size());
	CHECK(first.at("content_version") == "e43fc92e8a98e8429bf6436271b4a03fa7d929219cfac77a8d531bc12fb07303");
	CHECK(first.at("target") == target);
	CHECK(world.Text(own, target).contains("error"));
	CHECK_THROWS_AS(AdmitWorldTextStart(), InvalidParameters);
	CHECK(world.Text(world.companies[1], target).contains("error"));
	WaitForNewsText();
	const Json second = world.Text(own, target, {{"after_byte", 2047}, {"expected_content_version", first.at("content_version")}});
	REQUIRE_FALSE(second.contains("error"));
	CHECK(second.at("text") == text.substr(2047));
	CHECK(second.at("next_after_byte").is_null());
	CHECK(second.at("content_version") == first.at("content_version"));
	CHECK(world.Text(own, target, {{"after_byte", 2047}}).contains("error"));
	CHECK(world.Text(own, target, {{"cargo_id", 0}}).contains("error"));
	CHECK(world.Text(own, target, {{"limit", 2049}}).contains("error"));
	WaitForNewsText();
	CHECK(world.Text(own, target, {{"after_byte", 2048}, {"expected_content_version", first.at("content_version")}}).contains("error"));
	WaitForNewsText();
	CHECK(world.Text(own, target, {{"after_byte", 2047}, {"expected_content_version", std::string(64, '0')}}).contains("error"));
}

TEST_CASE("Custom news live inline names change text versions but explicit references stay historical", "[integration][custom-news][references]")
{
	CustomNewsWorld world;
	Company *own = world.companies[0];
	REQUIRE(Town::CanAllocateItem());
	std::unique_ptr<Town> town(Town::Create(TileXY(10, 10)));
	town->name = "Old town";
	const TownID slot = town->index;
	const Json historical = Handle(town.get());
	const EncodedString inline_name = GetEncodedString(STR_TOWN_NAME, slot);
	REQUIRE(world.Present(inline_name, own->index, NewsType::General, slot).Succeeded());
	const Json row = world.Page(own).at("items").at(0);
	CHECK(row.at("reference") == Json{{"kind", "town"}, {"target", historical}, {"location", {{"tile", TileXY(10, 10).base()}, {"x", 10}, {"y", 10}}}});
	WaitForNewsText();
	const Json old_text = world.Text(own, row.at("news_id"));
	REQUIRE_FALSE(old_text.contains("error"));
	CHECK(old_text.at("text") == "Old town");
	town->name = "Renamed town";
	WaitForNewsText();
	CHECK(world.Text(own, row.at("news_id"), {{"after_byte", 1}, {"expected_content_version", old_text.at("content_version")}}).contains("error"));
	WaitForNewsText();
	const Json renamed = world.Text(own, row.at("news_id"));
	REQUIRE_FALSE(renamed.contains("error"));
	CHECK(renamed.at("text") == "Renamed town");
	CHECK(renamed.at("content_version") != old_text.at("content_version"));
	town.reset();
	town.reset(Town::CreateAtIndex(slot, TileXY(11, 11)));
	town->name = "Replacement town";
	REQUIRE(Handle(town.get()) != historical);
	const Json after_reuse = world.Page(own).at("items").at(0);
	CHECK(after_reuse.at("reference") == row.at("reference"));
	CHECK(after_reuse.at("news_id") == row.at("news_id"));
	WaitForNewsText();
	const Json replacement = world.Text(own, row.at("news_id"));
	REQUIRE_FALSE(replacement.contains("error"));
	CHECK(replacement.at("text") == "Replacement town");
	CHECK(replacement.at("content_version") != renamed.at("content_version"));
}

TEST_CASE("Custom news issuance and listing never decode owner-dependent engine names", "[integration][custom-news][native-callback]")
{
	CustomNewsWorld world;
	Company *own = world.companies[0], *other = world.companies[1];
	GRFFile grf{};
	grf.grf_version = 8;
	grf.grfid = GrfID{"\x43\x4E\x45\x57"};
	REQUIRE(Engine::CanAllocateItem());
	std::unique_ptr<Engine> engine(Engine::Create(VehicleType::Road, 0));
	engine->info.callback_mask.Set(VehicleCallbackMask::Name);
	SetEngineGRF(engine->index, &grf);
	AddGRFString(grf.grfid, GRFSTR_MISC_GRF_TEXT, static_cast<uint8_t>(GRFLanguage::English), true, true, "Company A model", STR_UNDEFINED);
	AddGRFString(grf.grfid, GRFSTR_MISC_GRF_TEXT + 1, static_cast<uint8_t>(GRFLanguage::English), true, true, "Company B model", STR_UNDEFINED);
	REQUIRE(SpriteGroup::CanAllocateItem());
	std::unique_ptr<OwnerNameGroup> group(OwnerNameGroup::Create(own->index));
	engine->grf_prop.SetSpriteGroup(CargoGRFFileProps::SG_DEFAULT, group.get());
	const EncodedString text = GetEncodedString(STR_ENGINE_NAME, PackEngineNameDParam(engine->index, EngineNameContext::Generic));
	_current_company = own->index;
	CHECK(text.GetDecodedString() == "Company A model");
	const unsigned native_calls = group->Count();
	REQUIRE(native_calls > 0);
	group->calls.fill(0);
	const Randomizer random = _random, interactive_random = _interactive_random;
	const size_t psa_count = PersistentStorage::GetNumItems();
	const Money money = own->money;
	REQUIRE(world.Present(text, own->index).Succeeded());
	world.socket.Value(std::nullopt);
	const CommandCost disabled = world.Present(text, own->index);
	REQUIRE(disabled.Succeeded());
	CHECK(disabled.GetCost() == 0);
	CHECK(group->Count() == 0);
	world.socket.Value("/openttd-custom-news-regression/no-listener.sock");
	CHECK(world.Page(own).at("latest").at("id") == 1);
	while (Company::CanAllocateItem()) world.NewCompany();
	REQUIRE(Company::GetNumItems() == MAX_COMPANIES);
	REQUIRE(world.Present(text, INVALID_OWNER).Succeeded());
	CHECK(group->Count() == 0);
	CHECK(_current_company == OWNER_DEITY);
	CHECK(_local_company == COMPANY_SPECTATOR);
	const Json a = world.Page(own), b = world.Page(other);
	REQUIRE(a.at("items").size() == 2);
	REQUIRE(b.at("items").size() == 1);
	CHECK(group->Count() == 0);
	for (Company *company : Company::Iterate()) {
		const Json page = world.Page(company);
		REQUIRE_FALSE(page.at("items").empty());
		CheckNewsMetadata(page.at("items").back());
		CHECK(group->Count() == 0);
	}
	_current_company = own->index;
	CheckNewsMetadata(a.at("items").at(0));
	CHECK(CustomNewsText(a.at("items").at(0).at("news_id")) == text);
	CHECK(group->Count() == 0);
	WaitForNewsText();
	AdmitWorldTextStart(); // Denial must happen before any native formatter callback.
	CHECK(world.Text(own, a.at("items").at(0).at("news_id")).contains("error"));
	CHECK(group->Count() == 0);
	WaitForNewsText();
	const Json own_text = world.Text(own, a.at("items").at(0).at("news_id"));
	REQUIRE_FALSE(own_text.contains("error"));
	CHECK(own_text.at("text") == "Company A model");
	CHECK(group->Count() == native_calls);
	CHECK(group->calls[own->index.base()] == native_calls);
	CHECK(_current_company == own->index);
	group->calls.fill(0);
	WaitForNewsText();
	const Json other_text = world.Text(other, b.at("items").at(0).at("news_id"));
	REQUIRE_FALSE(other_text.contains("error"));
	CHECK(other_text.at("text") == "Company B model");
	CHECK(group->Count() == native_calls);
	CHECK(group->calls[other->index.base()] == native_calls);
	CHECK(_current_company == other->index);
	CHECK(own->money == money);
	CHECK(PersistentStorage::GetNumItems() == psa_count);
	for (size_t i = 0; i < 2; ++i) {
		CHECK(_random.state[i] == random.state[i]);
		CHECK(_interactive_random.state[i] == interactive_random.state[i]);
	}
	engine->grf_prop.SetSpriteGroup(CargoGRFFileProps::SG_DEFAULT, nullptr);
	CleanUpStrings();
}
