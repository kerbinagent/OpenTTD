/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License version 2.
 */

/** @file test_integration_story.cpp GameScript presentation visibility and freshness boundaries. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions_internal.h"
#include "../integration/actions_story.h"
#include "../integration/actions_spatial.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../core/endian_func.hpp"
#include "../core/pool_func.hpp"
#include "../engine_base.h"
#include "../goal_base.h"
#include "../goal_cmd.h"
#include "../language.h"
#include "../misc/endian_buffer.hpp"
#include "../network/network_base.h"
#include "../roadveh.h"
#include "../story_base.h"
#include "../story_cmd.h"
#include "../strings_func.h"

#include <fstream>
#include <thread>

#include "table/strings.h"
#include "../safeguards.h"

namespace {

using namespace Integration;
using namespace Integration::Detail;

void EnsureStoryLanguage()
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

struct StoryWorld {
	CompanyID previous_company = _current_company;
	Company *company;
	Company *other;

	StoryWorld()
	{
		EnsureStoryLanguage();
		ResetStoryPresentations();
		_story_page_element_pool.CleanPool();
		_story_page_pool.CleanPool();
		_goal_pool.CleanPool();
		_vehicle_pool.CleanPool();
		_networkclientinfo_pool.CleanPool();
		_company_pool.CleanPool();
		Map::Allocate(64, 64);
		REQUIRE(Company::CanAllocateItem());
		company = Company::Create();
		REQUIRE(Company::CanAllocateItem());
		other = Company::Create();
		_current_company = company->index;
	}

	~StoryWorld()
	{
		ResetStoryPresentations();
		_story_page_element_pool.CleanPool();
		_story_page_pool.CleanPool();
		_goal_pool.CleanPool();
		_vehicle_pool.CleanPool();
		_networkclientinfo_pool.CleanPool();
		_company_pool.CleanPool();
		_current_company = previous_company;
	}

	StoryPage *Page(CompanyID owner) const
	{
		REQUIRE(StoryPage::CanAllocateItem());
		return StoryPage::Create(uint32_t{1}, TimerGameCalendar::date, owner, GetEncodedString(STR_NULL, std::string_view("title")));
	}

	StoryPageElement *Element(StoryPage *page, StoryPageElementType type = StoryPageElementType::ButtonPush) const
	{
		REQUIRE(StoryPageElement::CanAllocateItem());
		return StoryPageElement::Create(uint32_t{1}, type, page->index);
	}

	NetworkClientInfo *Client(uint32_t id, CompanyID company) const
	{
		REQUIRE(NetworkClientInfo::CanAllocateItem());
		auto *client = NetworkClientInfo::Create(static_cast<ClientID>(id));
		client->client_playas = company;
		return client;
	}
};

template <Commands Tcommand, typename ... T>
CommandDataBuffer NativeData(T &&... values)
{
	using Args = typename CommandTraits<Tcommand>::Args;
	return EndianBufferWriter<CommandDataBuffer>::FromValue(Args{std::forward<T>(values)...});
}

Json QuestionParameters(uint16_t id, uint64_t generation, const char *button = "yes")
{
	return {{"question_id", {{"id", id}, {"generation", generation}, {"engine_instance_id", InstanceId()}}}, {"button", button}};
}

TargetWatermarks CurrentWatermarks()
{
	TargetWatermarks result;
	result.company = Company::GetLastInstanceGeneration();
	result.story_page = StoryPage::GetLastInstanceGeneration();
	result.story_element = StoryPageElement::GetLastInstanceGeneration();
	result.goal = Goal::GetLastInstanceGeneration();
	result.vehicle = Vehicle::GetLastInstanceGeneration();
	result.question = LastStoryQuestionGeneration();
	return result;
}

} // namespace

TEST_CASE("Text questions pin an offered issuance and never reuse handles across reset", "[integration][story]")
{
	StoryWorld world;
	constexpr uint16_t id = 17;
	StoryQuestionPresented(id, world.company->index.base(), false, GoalQuestionButtons{GoalQuestionButton::Yes}, GoalQuestionType::Question, {});
	const auto issued = LastStoryQuestionGeneration();
	const auto parameters = QuestionParameters(id, issued);
	CHECK(CaptureStoryTargets("goal.question.answer", parameters).has_value());
	CHECK_THROWS_AS(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, issued, "cancel")), InvalidParameters);
	StoryQuestionAnswered(world.other->index, id);
	CHECK(CaptureStoryTargets("goal.question.answer", parameters).has_value());
	StoryQuestionAnswered(world.company->index, id);
	CHECK_THROWS_AS(CaptureStoryTargets("goal.question.answer", parameters), InvalidParameters);
	ResetStoryPresentations();
	CHECK(LastStoryQuestionGeneration() == issued);
	StoryQuestionPresented(id, world.company->index.base(), false, GoalQuestionButtons{GoalQuestionButton::Yes}, GoalQuestionType::Question, {});
	CHECK(LastStoryQuestionGeneration() > issued);
	CHECK_THROWS_AS(CaptureStoryTargets("goal.question.answer", parameters), InvalidParameters);
}

TEST_CASE("Control references expose only selected handles and owned order mutation tokens", "[integration][control-reference]")
{
	StoryWorld world;
	std::this_thread::sleep_for(std::chrono::milliseconds(1100));
	REQUIRE(RoadVehicle::CanAllocateItem());
	auto *vehicle = RoadVehicle::Create();
	vehicle->owner = world.company->index;
	vehicle->SetFrontEngine();
	vehicle->unitnumber = 9;
	const Json selection = {{"kind", "vehicle"}, {"vehicle_type", "road"}, {"unit_number", 9}};
	const Json context = ResolveControlReference(selection);
	const Json direct_selection = {{"kind", "vehicle"}, {"id", vehicle->index.base()}};
	CHECK_THROWS_AS(ResolveControlReference(direct_selection), InvalidParameters);
	REQUIRE(context.size() == 3);
	CHECK(context.at("reference") == Handle(vehicle));
	CHECK(context.at("order_version") == VehicleOrderControlVersion(vehicle));
	vehicle->tile = TileXY(12, 13);
	vehicle->cur_speed = 37;
	std::this_thread::sleep_for(std::chrono::milliseconds(1100));
	CHECK(ResolveControlReference(direct_selection) == context);
	CHECK(CaptureActionTargets("order.insert", {{"vehicle_id", context.at("reference")}, {"order_version", context.at("order_version")},
		{"destination_type", "nearest_depot"}}).is_array());

	REQUIRE(RoadVehicle::CanAllocateItem());
	auto *part = RoadVehicle::Create();
	part->owner = world.company->index;
	const Json own_part = ResolveControlReference({{"kind", "vehicle"}, {"id", part->index.base()}});
	CHECK(own_part.size() == 2);
	CHECK(own_part.at("reference") == Handle(part));
	part->owner = world.other->index;
	CHECK_THROWS_AS(ResolveControlReference({{"kind", "vehicle"}, {"id", part->index.base()}}), InvalidParameters);
	part->SetFrontEngine();
	CHECK(ResolveControlReference({{"kind", "vehicle"}, {"id", part->index.base()}}).size() == 2);
	CHECK_THROWS_AS(ResolveControlReference({{"kind", "vehicle"}, {"vehicle_type", "road"}, {"unit_number", 0}}), InvalidParameters);
	CHECK_THROWS_AS(ResolveControlReference({{"kind", "vehicle"}, {"id", vehicle->index.base()}, {"name", "extra"}}), InvalidParameters);
	CHECK_THROWS_AS(ResolveControlReference({{"kind", "vehicle"}, {"id", true}}), InvalidParameters);

	part->owner = world.company->index;
	part->orders = vehicle->orders;
	if (vehicle->orders == nullptr) REQUIRE(OrderList::CanAllocateItem());
	part->AddToShared(vehicle);
	std::this_thread::sleep_for(std::chrono::milliseconds(1100));
	CHECK(ResolveControlReference(direct_selection).at("order_version") != context.at("order_version"));
	CHECK(CaptureActionTargets("order.insert", {{"vehicle_id", context.at("reference")}, {"order_version", context.at("order_version")},
		{"destination_type", "nearest_depot"}}).contains("error"));
}

TEST_CASE("Control presentation references hide content and retain current page and issuance fences", "[integration][control-reference]")
{
	StoryWorld world;
	std::this_thread::sleep_for(std::chrono::milliseconds(1100));
	auto *page = world.Page(world.company->index);
	auto *button = world.Element(page);
	const Json selected = ResolveControlReference({{"kind", "story_element"}, {"id", button->index.base()}});
	CHECK(selected.size() == 4);
	CHECK(selected.at("reference") == Handle(button));
	CHECK(selected.at("page") == Handle(page));
	const Json parameters = {{"element_id", selected.at("reference")}, {"page_version", selected.at("page_version")}};
	CHECK(CaptureStoryTargets("story.button.press", parameters).has_value());
	button->text = GetEncodedString(STR_NULL, std::string_view("changed instruction"));
	std::this_thread::sleep_for(std::chrono::milliseconds(1100));
	CHECK(ResolveControlReference({{"kind", "story_element"}, {"id", button->index.base()}}).at("page_version") != selected.at("page_version"));
	CHECK_THROWS_AS(CaptureStoryTargets("story.button.press", parameters), InvalidParameters);
	_current_company = world.other->index;
	CHECK_THROWS_AS(ResolveControlReference({{"kind", "story_element"}, {"id", button->index.base()}}), InvalidParameters);
	_current_company = world.company->index;
	StoryQuestionPresented(77, world.company->index.base(), false, GoalQuestionButtons{GoalQuestionButton::Yes}, GoalQuestionType::Question, {});
	const Json question = ResolveControlReference({{"kind", "question"}, {"id", 77}});
	CHECK(question.size() == 2);
	CHECK(question.at("reference").at("generation") == LastStoryQuestionGeneration());
	StoryQuestionPresented(77, world.company->index.base(), false, GoalQuestionButtons{GoalQuestionButton::No}, GoalQuestionType::Warning, {});
	CHECK_THROWS_AS(ResolveControlReference({{"kind", "question"}, {"id", 77}}), InvalidParameters);
}

TEST_CASE("Engine control references retain expired models and reject ambiguous names without attributes", "[integration][control-reference]")
{
	StoryWorld world;
	REQUIRE(Engine::CanAllocateItem(2));
	std::unique_ptr<Engine> first(Engine::Create(VehicleType::Road, 0));
	std::unique_ptr<Engine> second(Engine::Create(VehicleType::Road, 0));
	first->name = "Actuator model unique 20261017";
	second->name = "Another actuator model 20261017";
	first->company_avail = {};
	const Json direct = ResolveControlReference({{"kind", "engine"}, {"id", first->index.base()}});
	CHECK(direct == Json{{"kind", "engine"}, {"engine_id", first->index.base()}});
	const Json named = {{"kind", "engine"}, {"vehicle_type", "road"}, {"name", first->name}};
	std::this_thread::sleep_for(std::chrono::milliseconds(1100));
	CHECK(ResolveControlReference(named) == direct);
	second->name = first->name;
	std::this_thread::sleep_for(std::chrono::milliseconds(1100));
	CHECK_THROWS_AS(ResolveControlReference(named), InvalidParameters);
	CHECK(ResolveControlReference({{"kind", "engine"}, {"id", first->index.base()}}) == direct);
}

TEST_CASE("Typed native gameplay feedback stays within its UTF-8 acknowledgement budget", "[integration][control-reference]")
{
	StoryWorld world;
	std::string text = "x";
	for (uint i = 0; i < 600; ++i) text += "\u03bb";
	CommandCost failure(INVALID_STRING_ID);
	failure.SetEncodedMessage(GetEncodedString(STR_JUST_RAW_STRING, std::string_view(text)));
	const Json result = FormatCommandResult(Commands::BuildRoadStop, failure, {});
	CHECK_FALSE(result.at("success").get<bool>());
	CHECK(result.at("cost") == 0);
	CHECK(result.at("error").at("message").get_ref<const std::string &>().size() <= 1024);
	CHECK(result.at("error").at("message") == text.substr(0, 1023));
	CHECK_NOTHROW(result.dump());
}

TEST_CASE("Duplicate question issuances remain independently answerable with exact retirement", "[integration][story]")
{
	StoryWorld world;
	constexpr uint16_t id = 23;
	StoryQuestionPresented(id, CompanyID::Invalid().base(), false, GoalQuestionButtons{GoalQuestionButton::Yes}, GoalQuestionType::Question, {});
	const auto first = LastStoryQuestionGeneration();
	StoryQuestionPresented(id, world.company->index.base(), false, GoalQuestionButtons{GoalQuestionButton::No}, GoalQuestionType::Question, {});
	const auto second = LastStoryQuestionGeneration();
	CHECK(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, first)).has_value());
	CHECK(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, second, "no")).has_value());
	CHECK_THROWS_AS(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, second)), InvalidParameters);

	/* Selecting an older issue consumes exactly that issue, not the newer one. */
	StoryQuestionAnswered(world.company->index, id, first);
	CHECK_THROWS_AS(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, first)), InvalidParameters);
	CHECK(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, second, "no")).has_value());
	/* A selected issue already removed at execution never falls back. */
	StoryQuestionAnswered(world.company->index, id, first);
	CHECK(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, second, "no")).has_value());
	_current_company = world.other->index;
	CHECK(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, first)).has_value());
	CHECK_THROWS_AS(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, second, "no")), InvalidParameters);

	/* A native/deity close pops only one in each company's independent inbox. */
	_current_company = world.company->index;
	StoryQuestionPresented(id, CompanyID::Invalid().base(), false, GoalQuestionButtons{GoalQuestionButton::Yes}, GoalQuestionType::Question, {});
	const auto third = LastStoryQuestionGeneration();
	StoryQuestionsClosed(id);
	CHECK_THROWS_AS(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, third)), InvalidParameters);
	CHECK(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, second, "no")).has_value());
	_current_company = world.other->index;
	CHECK(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, first)).has_value());
	StoryQuestionAnswered(world.other->index, id);
	CHECK_THROWS_AS(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, first)), InvalidParameters);
}

TEST_CASE("Question window witnesses are private and independent of text inbox retirement", "[integration][story]")
{
	StoryWorld world;
	auto *client = world.Client(110, world.company->index);
	constexpr uint16_t id = 29;
	const auto answer = NativeData<Commands::GoalQuestionAnswer>(id, GoalQuestionButton::Yes);
	const auto witness = MakeStoryQuestionWitness(id, GoalQuestionType::Question, GoalQuestionButtons{GoalQuestionButton::Yes}, {});
	/* A client-private popup never creates a company text question, even with
	 * the same qid as another company's/public presentation. */
	StoryQuestionPresented(id, to_underlying(client->client_id), true, GoalQuestionButtons{GoalQuestionButton::Yes}, GoalQuestionType::Question, {});
	CHECK_THROWS_AS(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, LastStoryQuestionGeneration())), InvalidParameters);
	const auto valid_witness = ValidateStoryQuestionWitness(answer, witness, client->client_id);
	INFO(valid_witness.error);
	CHECK(valid_witness.accepted);
	NativeTileSet no_tiles;
	const auto spatial = NativeSpatialProof(Commands::GoalQuestionAnswer, answer, no_tiles);
	const Json state = {{"story_state", witness}};
	CHECK(VerifyNativeIntentProof(Commands::GoalQuestionAnswer, answer, CurrentWatermarks(), spatial, state, client->client_id).accepted);
	auto invalid_state = state;
	invalid_state["extra"] = true;
	CHECK_FALSE(VerifyNativeIntentProof(Commands::GoalQuestionAnswer, answer, CurrentWatermarks(), spatial, invalid_state, client->client_id).accepted);
	auto invalid_spatial = spatial;
	invalid_spatial["count"] = uint64_t{1};
	CHECK_FALSE(VerifyNativeIntentProof(Commands::GoalQuestionAnswer, answer, CurrentWatermarks(), invalid_spatial, state, client->client_id).accepted);
	StoryQuestionsClosed(id);
	CHECK(ValidateStoryQuestionWitness(answer, witness, client->client_id).accepted);
	CHECK_THROWS_AS(StoryIntentState(Commands::GoalQuestionAnswer, answer, client->client_id), InvalidParameters);
	CHECK_FALSE(ValidateStoryQuestionWitness(answer, witness, ClientID::Invalid).accepted);
	client->client_playas = world.other->index;
	CHECK_FALSE(ValidateStoryQuestionWitness(answer, witness, client->client_id).accepted);
	client->client_playas = world.company->index;

	for (const char *field : {"source", "question_id", "type", "buttons", "text_hash"}) {
		auto missing = witness;
		missing.erase(field);
		CHECK_FALSE(ValidateStoryQuestionWitness(answer, missing, client->client_id).accepted);
	}
	auto invalid = witness;
	invalid["extra"] = true;
	CHECK_FALSE(ValidateStoryQuestionWitness(answer, invalid, client->client_id).accepted);
	invalid = witness;
	invalid["question_id"] = id + 1;
	CHECK_FALSE(ValidateStoryQuestionWitness(answer, invalid, client->client_id).accepted);
	invalid = witness;
	invalid["buttons"] = GoalQuestionButtons{GoalQuestionButton::No}.base();
	CHECK_FALSE(ValidateStoryQuestionWitness(answer, invalid, client->client_id).accepted);
	invalid = witness;
	invalid["buttons"] = UINT32_MAX;
	CHECK_FALSE(ValidateStoryQuestionWitness(answer, invalid, client->client_id).accepted);
	invalid = witness;
	invalid["text_hash"] = std::string(64, 'Z');
	CHECK_FALSE(ValidateStoryQuestionWitness(answer, invalid, client->client_id).accepted);
	CHECK_FALSE(ValidateStoryQuestionWitness(NativeData<Commands::GoalQuestionAnswer>(id, GoalQuestionButton::No), witness, client->client_id).accepted);
}

TEST_CASE("Question cursors page duplicate issuances without skipping or repeating", "[integration][story]")
{
	StoryWorld world;
	constexpr uint16_t id = 42;
	std::vector<uint64_t> issued;
	for (uint i = 0; i < 40; ++i) {
		StoryQuestionPresented(id, world.company->index.base(), false, GoalQuestionButtons{GoalQuestionButton::Yes}, GoalQuestionType::Question, {});
		issued.push_back(LastStoryQuestionGeneration());
	}
	StoryQuestionPresented(id + 1, world.company->index.base(), false, GoalQuestionButtons{GoalQuestionButton::Yes}, GoalQuestionType::Question, {});
	issued.push_back(LastStoryQuestionGeneration());
	Json cursor = {{"after_id", id - 1}, {"limit", 7}};
	std::vector<uint64_t> seen;
	for (uint pages = 0; pages < 10; ++pages) {
		auto page = QueryStoryState("goal.questions", cursor);
		REQUIRE(page.has_value());
		CHECK(page->at("items").size() <= 7);
		for (const auto &item : page->at("items")) seen.push_back(item.at("question_id").at("generation").get<uint64_t>());
		REQUIRE(page->at("next_after_id").is_null() == page->at("next_after_generation").is_null());
		if (page->at("next_after_id").is_null()) break;
		cursor["after_id"] = page->at("next_after_id");
		cursor["after_generation"] = page->at("next_after_generation");
	}
	CHECK(seen == issued);
	/* Legacy after_id by itself still skips the entire qid. */
	auto legacy = QueryStoryState("goal.questions", {{"after_id", id}});
	REQUIRE(legacy->at("items").size() == 1);
	CHECK(legacy->at("items").at(0).at("question_id").at("id") == id + 1);
	auto within = QueryStoryState("goal.questions", {{"after_id", id}, {"after_generation", issued.at(38)}});
	REQUIRE(within->at("items").size() == 2);
	CHECK(within->at("items").at(0).at("question_id").at("generation") == issued.at(39));
	CHECK_THROWS_AS(QueryStoryState("goal.questions", {{"after_id", -1}, {"after_generation", 1}}), InvalidParameters);
	CHECK_THROWS_AS(QueryStoryState("goal.questions", {{"after_generation", -1}}), InvalidParameters);
	CHECK_THROWS_AS(QueryStoryState("goal.questions", {{"after_generation", 1.5}}), InvalidParameters);
	/* Unattributed native answers choose newest in this frontend only. */
	StoryQuestionAnswered(world.company->index, id);
	CHECK_THROWS_AS(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, issued.at(39))), InvalidParameters);
	CHECK(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, issued.front())).has_value());
}

TEST_CASE("A replacement company does not inherit an earlier text question inbox", "[integration][story]")
{
	StoryWorld world;
	constexpr uint16_t id = 50;
	StoryQuestionPresented(id, world.company->index.base(), false, GoalQuestionButtons{GoalQuestionButton::Yes}, GoalQuestionType::Question, {});
	const auto old_generation = LastStoryQuestionGeneration();
	const auto company_id = world.company->index;
	delete world.company;
	REQUIRE(Company::CanAllocateItem());
	world.company = Company::Create();
	REQUIRE(world.company->index == company_id);
	_current_company = world.company->index;
	CHECK_THROWS_AS(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, old_generation)), InvalidParameters);
	CHECK(QueryStoryState("goal.questions", {})->at("items").empty());
	StoryQuestionPresented(id, world.company->index.base(), false, GoalQuestionButtons{GoalQuestionButton::Yes}, GoalQuestionType::Question, {});
	CHECK(CaptureStoryTargets("goal.question.answer", QuestionParameters(id, LastStoryQuestionGeneration())).has_value());
}

TEST_CASE("Story proof binds instructions and displayed referenced goals without local generation equality", "[integration][story]")
{
	StoryWorld world;
	auto *page = world.Page(world.company->index);
	auto *button = world.Element(page);
	auto *instructions = world.Element(page, StoryPageElementType::Text);
	instructions->text = GetEncodedString(STR_NULL, std::string_view("original instructions"));
	REQUIRE(Goal::CanAllocateItem());
	auto *goal = Goal::Create();
	goal->text = GetEncodedString(STR_NULL, std::string_view("deliver cargo"));
	auto *goal_element = world.Element(page, StoryPageElementType::Goal);
	goal_element->referenced_id = goal->index.base();
	const auto data = NativeData<Commands::StoryPageButton>(TileIndex{}, button->index, VehicleID::Invalid());
	const auto original = StoryIntentState(Commands::StoryPageButton, data);
	REQUIRE(original.has_value());
	const auto watermarks = CurrentWatermarks();
	CHECK(ValidateStoryNativeTargets(Commands::StoryPageButton, data, watermarks)->accepted);
	instructions->text = GetEncodedString(STR_NULL, std::string_view("changed instructions"));
	CHECK(StoryIntentState(Commands::StoryPageButton, data) != original);
	instructions->text = GetEncodedString(STR_NULL, std::string_view("original instructions"));
	goal->completed = true;
	CHECK(StoryIntentState(Commands::StoryPageButton, data) != original);
	goal->completed = false;
	goal->text = GetEncodedString(STR_NULL, std::string_view("changed goal text"));
	CHECK(StoryIntentState(Commands::StoryPageButton, data) != original);
	goal->text = GetEncodedString(STR_NULL, std::string_view("deliver cargo"));
	goal->progress = GetEncodedString(STR_NULL, std::string_view("not displayed in story page"));
	CHECK(StoryIntentState(Commands::StoryPageButton, data) == original);
	const auto goal_id = goal->index;
	delete goal;
	REQUIRE(Goal::CanAllocateItem());
	goal = Goal::Create();
	REQUIRE(goal->index == goal_id);
	goal->text = GetEncodedString(STR_NULL, std::string_view("deliver cargo"));
	CHECK(StoryIntentState(Commands::StoryPageButton, data) == original);
	CHECK_FALSE(ValidateStoryNativeTargets(Commands::StoryPageButton, data, watermarks)->accepted);
	_current_company = world.other->index;
	CHECK_THROWS_AS(StoryIntentState(Commands::StoryPageButton, data), InvalidParameters);
}

TEST_CASE("Story loaded pages hash beyond creation limit and vehicle selectors accept another owner", "[integration][story]")
{
	StoryWorld world;
	auto *page = world.Page(CompanyID::Invalid());
	auto *button = world.Element(page, StoryPageElementType::ButtonVehicle);
	StoryPageButtonData button_data;
	button_data.SetVehicleType(VehicleType::Road);
	button->referenced_id = button_data.referenced_id;
	StoryPageElement *last = nullptr;
	for (uint i = 0; i < 130; ++i) last = world.Element(page, StoryPageElementType::Text);
	REQUIRE(last != nullptr);
	REQUIRE(RoadVehicle::CanAllocateItem());
	auto *vehicle = RoadVehicle::Create();
	vehicle->owner = world.other->index;
	vehicle->SetFrontEngine();
	vehicle->tile = TileXY(10, 10);
	const auto data = NativeData<Commands::StoryPageButton>(TileIndex{}, button->index, vehicle->index);
	const auto original = StoryIntentState(Commands::StoryPageButton, data);
	REQUIRE(original.has_value());
	CHECK(ValidateStoryNativeTargets(Commands::StoryPageButton, data, CurrentWatermarks())->accepted);
	vehicle->tile = TileXY(12, 12);
	vehicle->cur_speed = 25;
	CHECK(StoryIntentState(Commands::StoryPageButton, data) == original);
	last->text = GetEncodedString(STR_NULL, std::string_view("new instruction beyond element 128"));
	CHECK(StoryIntentState(Commands::StoryPageButton, data) != original);
	button_data.SetVehicleType(VehicleType::Train);
	button->referenced_id = button_data.referenced_id;
	CHECK_THROWS_AS(StoryIntentState(Commands::StoryPageButton, data), InvalidParameters);
}

TEST_CASE("Story armed placement versions bind page instructions and actual page permissions", "[integration][story]")
{
	StoryWorld world;
	auto *page = world.Page(CompanyID::Invalid());
	auto *button = world.Element(page, StoryPageElementType::ButtonTile);
	const auto original = StoryPlacementVersion(button->index);
	_current_company = world.other->index;
	/* A global page remains interactive when read in any company's book. */
	CHECK(StoryPlacementVersion(button->index) == original);
	_current_company = world.company->index;
	REQUIRE(Goal::CanAllocateItem());
	auto *goal = Goal::Create();
	auto *goal_element = world.Element(page, StoryPageElementType::Goal);
	goal_element->referenced_id = goal->index.base();
	const auto before_goal_change = StoryPlacementVersion(button->index);
	goal->completed = true;
	CHECK(StoryPlacementVersion(button->index) != before_goal_change);
	goal->completed = false;
	goal->text = GetEncodedString(STR_NULL, std::string_view("new instructions through a referenced goal"));
	CHECK(StoryPlacementVersion(button->index) != before_goal_change);
	const auto button_id = button->index;
	const auto before_replacement = StoryPlacementVersion(button_id);
	delete button;
	button = world.Element(page, StoryPageElementType::ButtonTile);
	REQUIRE(button->index == button_id);
	CHECK(StoryPlacementVersion(button_id) != before_replacement);
	auto *foreign = world.Element(world.Page(world.other->index), StoryPageElementType::ButtonVehicle);
	CHECK_THROWS_AS(StoryPlacementVersion(foreign->index), InvalidParameters);
	button->type = StoryPageElementType::ButtonPush;
	CHECK_THROWS_AS(StoryPlacementVersion(button->index), InvalidParameters);
}

TEST_CASE("Story and question detail truncation describes the returned chunk rather than its preview", "[integration][story]")
{
	StoryWorld world;
	auto *page = world.Page(world.company->index);
	auto *element = world.Element(page, StoryPageElementType::Text);
	const std::string text(1000, 'x');
	element->text = GetEncodedString(STR_JUST_RAW_STRING, std::string_view(text));
	auto detail = QueryStoryState("story.element", {{"element_id", Handle(element)}});
	REQUIRE(detail.has_value());
	CHECK(detail->at("text").get<std::string>() == text);
	CHECK_FALSE(detail->at("text_truncated").get<bool>());
	CHECK(detail->at("next_text_offset").is_null());
	auto part = QueryStoryState("story.element", {{"element_id", Handle(element)}, {"text_limit", 512}});
	REQUIRE(part.has_value());
	CHECK(part->at("text_truncated").get<bool>());
	CHECK(part->at("next_text_offset") == 512);
	auto suffix = QueryStoryState("story.element", {{"element_id", Handle(element)}, {"text_offset", 250}});
	REQUIRE(suffix.has_value());
	CHECK(suffix->at("text").get<std::string>().size() == 750);
	CHECK(suffix->at("text_truncated").get<bool>());
	CHECK(suffix->at("next_text_offset").is_null());
	StoryQuestionPresented(125, world.company->index.base(), false, GoalQuestionButtons{GoalQuestionButton::Yes}, GoalQuestionType::Question, element->text);
	const Json question = QuestionParameters(125, LastStoryQuestionGeneration()).at("question_id");
	auto answer_text = QueryStoryState("goal.question", {{"question_id", question}});
	REQUIRE(answer_text.has_value());
	CHECK(answer_text->at("text").get<std::string>() == text);
	CHECK_FALSE(answer_text->at("text_truncated").get<bool>());
	element->text = GetEncodedString(STR_JUST_RAW_STRING, std::string_view("\xE2\x82\xAC" "B"));
	auto euro = QueryStoryState("story.element", {{"element_id", Handle(element)}, {"text_limit", 3}});
	REQUIRE(euro.has_value());
	CHECK(euro->at("text").get<std::string>() == "\xE2\x82\xAC");
	CHECK(euro->at("next_text_offset") == 3);
	CHECK_THROWS_AS(QueryStoryState("story.element", {{"element_id", Handle(element)}, {"text_offset", 1}}), InvalidParameters);
	CHECK_THROWS_AS(QueryStoryState("story.element", {{"element_id", Handle(element)}, {"text_limit", 1}}), InvalidParameters);
}
