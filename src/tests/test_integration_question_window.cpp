/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file test_integration_question_window.cpp Exact native question-window witnesses. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions_story.h"
#include "../integration/renderer_input.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../core/endian_func.hpp"
#include "../goal_gui.h"
#include "../gfx_func.h"
#include "../gui.h"
#include "../language.h"
#include "../openttd.h"
#include "../querystring_gui.h"
#include "../core/random_func.hpp"
#include "../strings_func.h"
#include "../window_gui.h"
#include "../window_func.h"
#include "../widgets/goal_widget.h"
#include "../widgets/town_widget.h"
#include "mock_environment.h"

#include <fstream>

#include "table/strings.h"
#include "../safeguards.h"

namespace {

struct QuestionWindows {
	DrawPixelInfo previous_screen = _screen;
	std::vector<uint8_t> pixels = std::vector<uint8_t>(1024 * 768);
	std::vector<Window *> windows;

	QuestionWindows()
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
		MockEnvironment::Instance();
		_screen.width = 1024;
		_screen.height = 768;
		_screen.pitch = 1024;
		_screen.dst_ptr = this->pixels.data();
		ScreenSizeChanged();
		SetupWidgetDimensions();
	}

	~QuestionWindows()
	{
		for (Window *window : this->windows) window->Close();
		Window::DeleteClosedWindows();
		_screen = this->previous_screen;
		ScreenSizeChanged();
	}

	Window *Show(uint16_t id, GoalQuestionType type, GoalQuestionButtons buttons, const EncodedString &text)
	{
		ShowGoalQuestion(id, type, buttons, text);
		for (Window *window : Window::Iterate()) {
			if (window->window_class != WindowClass::GoalQuestion || window->window_number != id ||
					std::ranges::find(this->windows, window) != this->windows.end()) continue;
			this->windows.push_back(window);
			return window;
		}
		FAIL("Native question window was not created");
		return nullptr;
	}
};

bool Live(const Window *wanted)
{
	for (const Window *window : Window::Iterate()) if (window == wanted) return true;
	return false;
}

/** Renderer startup role is process-local; restore it even when a check fails. */
struct RendererRole {
	std::optional<std::string> previous;
	RendererRole()
	{
		if (const char *value = std::getenv("OPENTTD_INTEGRATION_ROLE"); value != nullptr) this->previous = value;
#ifdef _WIN32
		REQUIRE(_putenv_s("OPENTTD_INTEGRATION_ROLE", "renderer") == 0);
#else
		REQUIRE(setenv("OPENTTD_INTEGRATION_ROLE", "renderer", 1) == 0);
#endif
	}
	~RendererRole()
	{
#ifdef _WIN32
		_putenv_s("OPENTTD_INTEGRATION_ROLE", this->previous.has_value() ? this->previous->c_str() : "");
#else
		if (this->previous.has_value()) setenv("OPENTTD_INTEGRATION_ROLE", this->previous->c_str(), 1);
		else unsetenv("OPENTTD_INTEGRATION_ROLE");
#endif
	}
};


} // namespace

TEST_CASE("Native duplicate question witnesses use the exact popup and offered widget", "[integration][question-window]")
{
	QuestionWindows fixture;
	const auto older_text = GetEncodedString(STR_JUST_RAW_STRING, std::string_view("Older: choose yes"));
	const auto newer_text = GetEncodedString(STR_JUST_RAW_STRING, std::string_view("Newer: choose no"));
	Window *older = fixture.Show(4200, GoalQuestionType::Question, {GoalQuestionButton::Yes}, older_text);
	Window *newer = fixture.Show(4200, GoalQuestionType::Warning, {GoalQuestionButton::No}, newer_text);
	REQUIRE(older != newer);
	REQUIRE(Live(older));
	REQUIRE(Live(newer));
	const auto older_witness = Integration::RendererGoalQuestionWindowWitness(older, WID_GQ_BUTTON_1, 4200, GoalQuestionButton::Yes);
	const auto newer_witness = Integration::RendererGoalQuestionWindowWitness(newer, WID_GQ_BUTTON_1, 4200, GoalQuestionButton::No);
	REQUIRE(older_witness.has_value());
	REQUIRE(newer_witness.has_value());
	CHECK(*older_witness == Integration::Detail::MakeStoryQuestionWitness(4200, GoalQuestionType::Question, {GoalQuestionButton::Yes}, older_text));
	CHECK(*newer_witness == Integration::Detail::MakeStoryQuestionWitness(4200, GoalQuestionType::Warning, {GoalQuestionButton::No}, newer_text));
	CHECK(older_witness->at("text_hash") != newer_witness->at("text_hash"));
	CHECK_FALSE(Integration::RendererGoalQuestionWindowWitness(older, WID_GQ_BUTTON_1, 4200, GoalQuestionButton::No).has_value());
	CHECK_FALSE(Integration::RendererGoalQuestionWindowWitness(newer, WID_GQ_BUTTON_1, 4201, GoalQuestionButton::No).has_value());
	CHECK_FALSE(Integration::RendererGoalQuestionWindowWitness(newer, WID_GQ_BUTTON_2, 4200, GoalQuestionButton::No).has_value());
	CHECK_FALSE(Integration::RendererGoalQuestionWindowWitness(newer, WID_GQ_CAPTION, 4200, GoalQuestionButton::No).has_value());
	CHECK_FALSE(Integration::RendererGoalQuestionWindowWitness(nullptr, WID_GQ_BUTTON_1, 4200, GoalQuestionButton::No).has_value());
	/* The copied content remains usable after native Close removes its source. */
	newer->Close();
	CHECK_FALSE(Live(newer));
	CHECK(Live(older));
	CHECK(*newer_witness == Integration::Detail::MakeStoryQuestionWitness(4200, GoalQuestionType::Warning, {GoalQuestionButton::No}, newer_text));
	CHECK_FALSE(Integration::RendererQuestionCommandWitness(4200, GoalQuestionButton::Yes).has_value());
}

TEST_CASE("Native question witnesses map each offered button and reject informational answers", "[integration][question-window]")
{
	QuestionWindows fixture;
	const auto text = GetEncodedString(STR_JUST_RAW_STRING, std::string_view("Choose a displayed button"));
	Window *question = fixture.Show(4201, GoalQuestionType::Question,
		{GoalQuestionButton::Cancel, GoalQuestionButton::Yes, GoalQuestionButton::Retry}, text);
	CHECK(Integration::RendererGoalQuestionWindowWitness(question, WID_GQ_BUTTON_1, 4201, GoalQuestionButton::Cancel).has_value());
	CHECK(Integration::RendererGoalQuestionWindowWitness(question, WID_GQ_BUTTON_2, 4201, GoalQuestionButton::Yes).has_value());
	CHECK(Integration::RendererGoalQuestionWindowWitness(question, WID_GQ_BUTTON_3, 4201, GoalQuestionButton::Retry).has_value());
	CHECK_FALSE(Integration::RendererGoalQuestionWindowWitness(question, WID_GQ_BUTTON_2, 4201, GoalQuestionButton::Retry).has_value());
	Window *information = fixture.Show(4202, GoalQuestionType::Information, {}, text);
	CHECK_FALSE(Integration::RendererGoalQuestionWindowWitness(information, WID_GQ_BUTTON_1, 4202, GoalQuestionButton::Cancel).has_value());
}

TEST_CASE("Question button drag is rejected before native dispatch and preserves its source", "[integration][question-window]")
{
	QuestionWindows fixture;
	RendererRole role;
	const GameMode previous_mode = _game_mode;
	const CompanyID previous_company = _current_company;
	const CompanyID previous_local = _local_company;
	REQUIRE(Company::CanAllocateItem());
	Company *company = Company::Create();
	struct Restore {
		GameMode mode;
		CompanyID company;
		CompanyID local;
		Company *created;
		~Restore() { _game_mode = mode; _current_company = company; _local_company = local; delete created; }
	} restore{previous_mode, previous_company, previous_local, company};
	_game_mode = GameMode::Normal;
	_current_company = _local_company = company->index;
	Window *question = fixture.Show(4203, GoalQuestionType::Question, {GoalQuestionButton::Yes},
		GetEncodedString(STR_JUST_RAW_STRING, std::string_view("A drag must not dismiss this question")));
	const Rect rect = question->GetWidget<NWidgetBase>(WID_GQ_BUTTON_1)->GetCurrentRect();
	const int x = question->left + (rect.left + rect.right) / 2;
	const int y = question->top + (rect.top + rect.bottom) / 2;
	REQUIRE(FindWindowFromPt(x, y) == question);
	CHECK_FALSE(Integration::RendererApplyInput({{"gesture", "drag"}, {"button", "left"}, {"x", x}, {"y", y}, {"end_x", x + 1}, {"end_y", y}}));
	CHECK(Live(question));
	CHECK_FALSE(_left_button_down);
	CHECK_FALSE(_left_button_clicked);
	CHECK_FALSE(Integration::RendererQuestionCommandWitness(4203, GoalQuestionButton::Yes).has_value());
}

TEST_CASE("Native town funding accepts only its concrete inline name editbox", "[integration][question-window][town-name]")
{
	QuestionWindows fixture;
	RendererRole role;
	REQUIRE(Company::CanAllocateItem());
	Company *company = Company::Create();
	Window *town_window = nullptr;
	struct RestoreTownWindow {
		GameMode mode = _game_mode;
		CompanyID current = _current_company;
		CompanyID local = _local_company;
		SavedRandomSeeds seeds;
		Window *&window;
		Company *company;
		RestoreTownWindow(Window *&window, Company *company) : window(window), company(company) { SaveRandomSeeds(&this->seeds); }
		~RestoreTownWindow()
		{
			if (this->window != nullptr && Live(this->window)) this->window->Close();
			Window::DeleteClosedWindows();
			delete this->company;
			_game_mode = this->mode;
			_current_company = this->current;
			_local_company = this->local;
			RestoreRandomSeeds(this->seeds);
		}
	} restore(town_window, company);
	_game_mode = GameMode::Normal;
	_current_company = _local_company = company->index;
	ShowFoundTownWindow();
	town_window = FindWindowById(WindowClass::FoundTown, 0);
	REQUIRE(town_window != nullptr);
	QueryString *query = Integration::RendererFoundTownNameQuery(town_window);
	REQUIRE(query != nullptr);
	REQUIRE(query == town_window->GetQueryString(WID_TF_TOWN_NAME_EDITBOX));
	const Rect rect = town_window->GetWidget<NWidgetBase>(WID_TF_TOWN_NAME_EDITBOX)->GetCurrentRect();
	const int x = town_window->left + (rect.left + rect.right) / 2;
	const int y = town_window->top + (rect.top + rect.bottom) / 2;
	REQUIRE(FindWindowFromPt(x, y) == town_window);
	const std::string name = "St. \xC3\x89loi";
	CHECK(Integration::RendererApplyInput({{"gesture", "text"}, {"x", x}, {"y", y}, {"text", name}}));
	CHECK(query->text.GetText() == name);
	CHECK_FALSE(Integration::RendererApplyInput({{"gesture", "text"}, {"x", x}, {"y", y}, {"text", std::string(query->text.max_chars + 1, 'A')}}));
	CHECK(query->text.GetText() == name);
	CHECK_FALSE(Integration::RendererApplyInput({{"gesture", "text"}, {"x", x}, {"y", y}, {"text", std::string(1, '\xff')}}));
	CHECK(query->text.GetText() == name);
	/* A click must not open the OSK, and text on a different native widget must
	 * not reuse the approved buffer through its window alone. */
	CHECK_FALSE(Integration::RendererApplyInput({{"gesture", "click"}, {"button", "left"}, {"x", x}, {"y", y}}));
	const Rect other_widget = town_window->GetWidget<NWidgetBase>(WID_TF_TOWN_NAME_RANDOM)->GetCurrentRect();
	CHECK_FALSE(Integration::RendererApplyInput({{"gesture", "text"},
		{"x", town_window->left + (other_widget.left + other_widget.right) / 2}, {"y", town_window->top + (other_widget.top + other_widget.bottom) / 2}, {"text", "wrong widget"}}));
	CHECK(query->text.GetText() == name);
	_game_mode = GameMode::Editor;
	CHECK(Integration::RendererFoundTownNameQuery(town_window) == nullptr);
	CHECK_FALSE(Integration::RendererApplyInput({{"gesture", "text"}, {"x", x}, {"y", y}, {"text", "editor denied"}}));
	_game_mode = GameMode::Normal;
	_local_company = COMPANY_SPECTATOR;
	CHECK(Integration::RendererFoundTownNameQuery(town_window) == nullptr);
	CHECK(Integration::RendererFoundTownNameQuery(nullptr) == nullptr);
}
