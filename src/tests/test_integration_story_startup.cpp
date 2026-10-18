/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License version 2.
 */

/** @file test_integration_story_startup.cpp Native GameScript questions before bridge initialization. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions_internal.h"
#include "../integration/actions_story.h"
#include "../integration/bridge_hooks.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../core/endian_func.hpp"
#include "../core/pool_func.hpp"
#include "../goal_cmd.h"
#include "../language.h"
#include "../network/network.h"
#include "../strings_func.h"
#include <fstream>

#include "table/strings.h"
#include "../safeguards.h"

namespace {

using namespace Integration;
using namespace Integration::Detail;

/** Restore even an originally empty environment value after each section. */
class ScopedEnvironment {
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
	explicit ScopedEnvironment(const char *name) : name(name)
	{
		if (const char *value = std::getenv(name); value != nullptr) this->previous = value;
	}
	~ScopedEnvironment() { Set(this->name, this->previous); }
	void Value(const std::optional<std::string> &value) { REQUIRE(Set(this->name, value) == 0); }
};

struct StartupNetworkContext {
	bool networking = _networking;
	bool server = _network_server;
	CompanyID company = _current_company;
	CompanyID local = _local_company;
	ScopedEnvironment socket{"OPENTTD_INTEGRATION_SOCKET"};
	ScopedEnvironment role{"OPENTTD_INTEGRATION_ROLE"};

	StartupNetworkContext()
	{
		this->role.Value(std::nullopt);
		this->socket.Value(std::nullopt);
	}
	~StartupNetworkContext()
	{
		_networking = this->networking;
		_network_server = this->server;
		_current_company = this->company;
		_local_company = this->local;
	}
};

struct StartupQuestionWorld {
	StartupNetworkContext context;
	Company *recipient = nullptr;
	static constexpr uint16_t QUESTION_ID = 3000;

	StartupQuestionWorld()
	{
		/* Real query decoding needs the built language resources, as native UI does. */
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
		/* Never Tick: this path is startup intent, with no listener or ledger. */
		this->context.socket.Value("/openttd-startup-regression/no-listener.sock");
		_networking = true;
		_network_server = true;
		_local_company = COMPANY_SPECTATOR;
		_current_company = OWNER_DEITY;
		REQUIRE(Company::CanAllocateItem());
		this->recipient = Company::Create();
	}

	~StartupQuestionWorld()
	{
		/* Delete only this fixture's objects; existing companies stay untouched. */
		StoryQuestionsClosed(QUESTION_ID);
		delete this->recipient;
	}

	CommandCost Present(DoCommandFlags flags) const
	{
		_current_company = OWNER_DEITY;
		return CmdGoalQuestion(flags, QUESTION_ID, this->recipient->index.base(), false,
			GoalQuestionButtons{GoalQuestionButton::Yes, GoalQuestionButton::No}, GoalQuestionType::Question,
			GetEncodedString(STR_JUST_RAW_STRING, std::string_view("startup question")));
	}

	Json Questions() const
	{
		_current_company = this->recipient->index;
		auto result = QueryStoryState("goal.questions", {{"after_id", QUESTION_ID - 1}, {"limit", 1}});
		REQUIRE(result.has_value());
		return std::move(*result);
	}
};

} // namespace

TEST_CASE("Presentation intent is available before bridge setup and excludes ordinary clients", "[integration][story][startup]")
{
	StartupNetworkContext context;
	_networking = true;
	_network_server = true;
	CHECK_FALSE(TracksPlayerPresentations());
	context.socket.Value("");
	CHECK_FALSE(TracksPlayerPresentations());
	context.socket.Value("/openttd-startup-regression/no-listener.sock");
	CHECK(TracksPlayerPresentations());
	_network_server = false;
	CHECK_FALSE(TracksPlayerPresentations()); // A native client may inherit the socket environment.
	_network_server = true;
	_networking = false;
	CHECK_FALSE(TracksPlayerPresentations());
	context.role.Value("renderer"); // Role activation is itself an initialization-free predicate.
	CHECK(TracksPlayerPresentations());
	context.role.Value("ordinary-client");
	CHECK_FALSE(TracksPlayerPresentations());
}

TEST_CASE("Native startup questions can be observed and retired before the first integration tick", "[integration][story][startup]")
{
	StartupQuestionWorld world;
	REQUIRE(TracksPlayerPresentations());
	REQUIRE_FALSE(Enabled()); // No configured bridge, listener or ledger, as at GameScript startup.
	const auto previous_issuance = LastStoryQuestionGeneration();
	REQUIRE(world.Present({}).Succeeded());
	CHECK(LastStoryQuestionGeneration() == previous_issuance);
	REQUIRE(world.Questions().at("items").empty());
	REQUIRE(world.Present(DoCommandFlag::Execute).Succeeded());
	CHECK(LastStoryQuestionGeneration() > previous_issuance);
	const Json shown = world.Questions();
	REQUIRE(shown.at("items").size() == 1);
	const Json handle = shown.at("items").at(0).at("question_id");
	CHECK(handle.at("id") == StartupQuestionWorld::QUESTION_ID);
	CHECK(shown.at("items").at(0).at("text") == "startup question");
	CHECK(shown.at("items").at(0).at("buttons") == Json::array({"no", "yes"}));
	const Json answer = {{"question_id", handle}, {"button", "yes"}};
	CHECK(CaptureStoryTargets("goal.question.answer", answer).has_value());
	CHECK(QueryStoryState("goal.question", {{"question_id", handle}}).has_value());

	SECTION("A native company answer retires the early presentation")
	{
		_current_company = world.recipient->index;
		REQUIRE(CmdGoalQuestionAnswer({}, StartupQuestionWorld::QUESTION_ID, GoalQuestionButton::Yes).Succeeded());
		CHECK(CaptureStoryTargets("goal.question.answer", answer).has_value());
		REQUIRE(CmdGoalQuestionAnswer(DoCommandFlag::Execute, StartupQuestionWorld::QUESTION_ID, GoalQuestionButton::Yes).Succeeded());
	}
	SECTION("A native deity close retires the early presentation")
	{
		_current_company = OWNER_DEITY;
		REQUIRE(CmdGoalQuestionAnswer(DoCommandFlag::Execute, StartupQuestionWorld::QUESTION_ID, GoalQuestionButton::Yes).Succeeded());
	}

	CHECK(world.Questions().at("items").empty());
	CHECK_THROWS_AS(CaptureStoryTargets("goal.question.answer", answer), InvalidParameters);
	CHECK_THROWS_AS(QueryStoryState("goal.question", {{"question_id", handle}}), InvalidParameters);
	CHECK_FALSE(Enabled());
}

TEST_CASE("Native answer and deity close consume one duplicate startup question each", "[integration][story][startup]")
{
	StartupQuestionWorld world;
	REQUIRE(world.Present(DoCommandFlag::Execute).Succeeded());
	const auto first = LastStoryQuestionGeneration();
	REQUIRE(world.Present(DoCommandFlag::Execute).Succeeded());
	const auto second = LastStoryQuestionGeneration();
	const auto first_answer = Json{{"question_id", {{"id", StartupQuestionWorld::QUESTION_ID}, {"generation", first}, {"engine_instance_id", InstanceId()}}}, {"button", "yes"}};
	const auto second_answer = Json{{"question_id", {{"id", StartupQuestionWorld::QUESTION_ID}, {"generation", second}, {"engine_instance_id", InstanceId()}}}, {"button", "yes"}};
	_current_company = OWNER_DEITY;
	REQUIRE(CmdGoalQuestionAnswer(DoCommandFlag::Execute, StartupQuestionWorld::QUESTION_ID, GoalQuestionButton::Yes).Succeeded());
	_current_company = world.recipient->index;
	CHECK(CaptureStoryTargets("goal.question.answer", first_answer).has_value());
	CHECK_THROWS_AS(CaptureStoryTargets("goal.question.answer", second_answer), InvalidParameters);
	REQUIRE(CmdGoalQuestionAnswer(DoCommandFlag::Execute, StartupQuestionWorld::QUESTION_ID, GoalQuestionButton::Yes).Succeeded());
	CHECK_THROWS_AS(CaptureStoryTargets("goal.question.answer", first_answer), InvalidParameters);
	CHECK(world.Questions().at("items").empty());
	CHECK_FALSE(Enabled());
}
