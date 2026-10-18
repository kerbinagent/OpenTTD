/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under
 * the terms of the GNU General Public License as published by the Free
 * Software Foundation, version 2.
 */

/** @file test_integration_company_settings_gui.cpp Native company-setting dialog provenance. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/renderer_input.h"
#include "../base_media_graphics.h"
#include "../base_media_music.h"
#include "../base_media_sounds.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../core/endian_func.hpp"
#include "../gfx_func.h"
#include "../gui.h"
#include "../language.h"
#include "../network/network.h"
#include "../openttd.h"
#include "../querystring_gui.h"
#include "../settingentry_gui.h"
#include "../settings_gui.h"
#include "../settings_internal.h"
#include "../settings_type.h"
#include "../textfile_type.h"
#include "../video/video_driver.hpp"
#include "../window_func.h"
#include "../window_gui.h"
#include "../widgets/misc_widget.h"
#include "../widgets/settings_widget.h"
#include "mock_environment.h"

#include <fstream>

#include "table/strings.h"
#include "../safeguards.h"

namespace {

/** Game Options sizes all three base-set labels even when only Advanced is
 * used. Supply scoped native base-set objects in a test process without a
 * scanned installation; restore the prior selected pointers on teardown. */
template <typename Media, typename Set>
struct ScopedBaseSet : Media {
	const Set *previous = Media::GetUsedSet();
	Set fixture{};
	ScopedBaseSet()
	{
		if (this->previous != nullptr) return;
		this->fixture.name = "native test base set";
		this->fixture.valid_files = Set::NUM_FILES;
		this->fixture.description[""] = this->fixture.name;
		Media::used_set = &this->fixture;
	}
	~ScopedBaseSet()
	{
		if (this->previous == nullptr) Media::used_set = nullptr;
	}
};

/** Match the normal window-test environment, with a real owned company. */
struct CompanySettingsWindows {
	ScopedBaseSet<BaseGraphics, GraphicsSet> graphics;
	ScopedBaseSet<BaseSounds, SoundsSet> sounds;
	ScopedBaseSet<BaseMusic, MusicSet> music;
	DrawPixelInfo previous_screen = _screen;
	CursorVars previous_cursor = _cursor;
	GameMode previous_mode = _game_mode;
	CompanyID previous_current = _current_company;
	CompanyID previous_local = _local_company;
	bool previous_networking = _networking;
	PauseModes previous_pause = _pause_mode;
	uint8_t previous_restriction = _settings_client.gui.settings_restriction_mode;
	std::vector<uint8_t> pixels = std::vector<uint8_t>(1024 * 768);
	Company *company = nullptr;
	Window *options = nullptr;

	CompanySettingsWindows()
	{
		Integration::RendererResetInputProvenance();
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
		/* A real Default QueryString needs the ordinary focus sink. Keep the
		 * test headless by installing the null video driver once. */
		if (VideoDriver::GetInstance() == nullptr) DriverFactoryBase::SelectDriver("null", Driver::Type::Video);
		_screen.width = 1024;
		_screen.height = 768;
		_screen.pitch = 1024;
		_screen.dst_ptr = this->pixels.data();
		ScreenSizeChanged();
		SetupWidgetDimensions();
		SetMouseCursor(SPR_CURSOR_MOUSE, PAL_NONE);
		_company_pool.CleanPool();
		Map::Allocate(64, 64);
		REQUIRE(Company::CanAllocateItem());
		this->company = Company::Create();
		_game_mode = GameMode::Normal;
		_current_company = _local_company = this->company->index;
		_networking = false;
		_pause_mode = {};
		_settings_client.gui.settings_restriction_mode = RM_BASIC;
	}

	~CompanySettingsWindows()
	{
		if (Window *query = FindWindowById(WindowClass::QueryString, QueryStringWindowNumber::Default); query != nullptr) query->Close();
		if (this->options != nullptr) this->options->Close();
		Window::DeleteClosedWindows();
		/* RendererRole may already have restored the environment by this point;
		 * reset the process-local lease explicitly after native window cleanup. */
		Integration::RendererResetInputProvenance();
		_company_pool.CleanPool();
		_game_mode = this->previous_mode;
		_current_company = this->previous_current;
		_local_company = this->previous_local;
		_networking = this->previous_networking;
		_pause_mode = this->previous_pause;
		_settings_client.gui.settings_restriction_mode = this->previous_restriction;
		_cursor = std::move(this->previous_cursor);
		_screen = this->previous_screen;
		ScreenSizeChanged();
	}

	void Open()
	{
		ShowGameOptions();
		this->options = FindWindowById(WindowClass::GameOptions, GameOptionsWindowNumber::GameOptions);
		REQUIRE(this->options != nullptr);
	}
};

/** The renderer role is process-local; never leave it enabled for other tests. */
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

Point Centre(const Rect &rect) { return {(rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2}; }

bool Click(Window *window, Point local)
{
	return Integration::RendererApplyInput({{"gesture", "click"}, {"button", "left"},
		{"x", window->left + local.x}, {"y", window->top + local.y}});
}

bool Move(Window *window, Point local)
{
	return Integration::RendererApplyInput({{"gesture", "move"},
		{"x", window->left + local.x}, {"y", window->top + local.y}});
}

bool ReplaceText(Window *query, std::string_view text)
{
	const Point local = Centre(query->GetWidget<NWidgetBase>(WID_QS_TEXT)->GetCurrentRect());
	return Integration::RendererApplyInput({{"gesture", "text"}, {"x", query->left + local.x},
		{"y", query->top + local.y}, {"text", std::string(text)}});
}

/** Find an actual visible row through the same native hit classifier as renderer input. */
std::optional<Point> FindSetting(Window *options, std::string_view name)
{
	Scrollbar *scrollbar = options->GetScrollbar(WID_GO_SCROLLBAR);
	const Rect panel = options->GetWidget<NWidgetBase>(WID_GO_OPTIONSPANEL)->GetCurrentRect();
	if (scrollbar == nullptr) return std::nullopt;
	const int x = panel.right - 8;
	for (Scrollbar::size_type position = 0; position < scrollbar->GetCount(); ++position) {
		scrollbar->SetPosition(position);
		for (int y = panel.top + 1; y < panel.bottom; ++y) {
			const Point local{x, y};
			const GameOptionsHit hit = ClassifyGameOptionsHit(options, local);
			if (hit.kind == GameOptionsHitKind::Setting && hit.setting.descriptor != nullptr && hit.setting.descriptor->GetName() == name) return local;
		}
	}
	return std::nullopt;
}

Window *OpenNumericQuery(Window *options, Point row)
{
	REQUIRE(Click(options, row));
	REQUIRE(Click(options, row));
	Window *query = FindWindowById(WindowClass::QueryString, QueryStringWindowNumber::Default);
	REQUIRE(query != nullptr);
	REQUIRE(query->parent == options);
	REQUIRE(query->GetQueryString(WID_QS_TEXT) != nullptr);
	return query;
}

} // namespace

TEST_CASE("Company settings Default query keeps its exact native row and text buffer", "[integration][company-settings-gui]")
{
	CompanySettingsWindows world;
	RendererRole role;
	world.Open();
	Window *options = world.options;
	const Point advanced = Centre(options->GetWidget<NWidgetBase>(WID_GO_TAB_ADVANCED)->GetCurrentRect());
	REQUIRE(Click(options, advanced));
	const int vertical_step = options->nested_root->resize_y;
	if (vertical_step > 0) ResizeWindow(options, 0, -vertical_step * 8, false, false);
	CHECK(_settings_client.gui.settings_restriction_mode == RM_BASIC);
	const auto row = FindSetting(options, "engine_renew_months");
	REQUIRE(row.has_value());
	const GameOptionsHit hit = ClassifyGameOptionsHit(options, *row);
	REQUIRE(hit.kind == GameOptionsHitKind::Setting);
	REQUIRE(hit.setting.descriptor != nullptr);
	Window *query = OpenNumericQuery(options, *row);
	const auto value_entry = CurrentGameOptionsValueEntry(options);
	REQUIRE(value_entry.has_value());
	CHECK(value_entry->entry == hit.setting.entry);
	CHECK(value_entry->descriptor == hit.setting.descriptor);
	QueryString *native_text = query->GetQueryString(WID_QS_TEXT);
	REQUIRE(ReplaceText(query, "7"));
	CHECK(std::string(native_text->text.GetText()) == "7");

	/* Scrolling changes pixels but not the selected SettingEntry or dialog lease. */
	Scrollbar *scrollbar = options->GetScrollbar(WID_GO_SCROLLBAR);
	REQUIRE(scrollbar->GetCount() > scrollbar->GetCapacity());
	const auto before_scroll = scrollbar->GetPosition();
	scrollbar->SetPosition(before_scroll == 0 ? scrollbar->GetCount() : 0);
	REQUIRE(scrollbar->GetPosition() != before_scroll);
	REQUIRE(ReplaceText(query, "8"));
	CHECK(std::string(native_text->text.GetText()) == "8");
	CHECK(CurrentGameOptionsValueEntry(options)->entry == value_entry->entry);
	/* A true renderer result proves admission to the normal native handler.
	 * Command capture and company-value readback belong to the live bridge fixture. */
	CHECK(Click(query, Centre(query->GetWidget<NWidgetBase>(WID_QS_OK)->GetCurrentRect())));
}

TEST_CASE("Company settings view exposes only the eight policy rows and safe navigation", "[integration][company-settings-gui]")
{
	CompanySettingsWindows world;
	RendererRole role;
	world.Open();
	Window *options = world.options;
	REQUIRE(Click(options, Centre(options->GetWidget<NWidgetBase>(WID_GO_TAB_ADVANCED)->GetCurrentRect())));
	CHECK(_settings_client.gui.settings_restriction_mode == RM_BASIC);
	for (std::string_view name : {"engine_renew", "engine_renew_months", "engine_renew_money",
			"vehicle.servint_ispercent", "vehicle.servint_trains", "vehicle.servint_roadveh",
			"vehicle.servint_ships", "vehicle.servint_aircraft"}) {
		INFO(name);
		const auto row = FindSetting(options, name);
		REQUIRE(row.has_value());
		const GameOptionsHit hit = ClassifyGameOptionsHit(options, *row);
		REQUIRE(hit.kind == GameOptionsHitKind::Setting);
		REQUIRE(hit.setting.descriptor != nullptr);
		CHECK(hit.setting.descriptor->GetName() == name);
		CHECK(Move(options, *row));
	}
	for (WidgetID denied : {WID_GO_TYPE_DROPDOWN, WID_GO_RESTRICT_DROPDOWN, WID_GO_RESET_ALL, WID_GO_TAB_GENERAL}) {
		INFO(denied);
		CHECK_FALSE(Click(options, Centre(options->GetWidget<NWidgetBase>(denied)->GetCurrentRect())));
	}
	CHECK(_settings_client.gui.settings_restriction_mode == RM_BASIC);
}

TEST_CASE("Company settings dialog rejects changed parent and text buffer identities", "[integration][company-settings-gui]")
{
	CompanySettingsWindows world;
	RendererRole role;
	world.Open();
	Window *options = world.options;
	REQUIRE(Click(options, Centre(options->GetWidget<NWidgetBase>(WID_GO_TAB_ADVANCED)->GetCurrentRect())));
	const auto row = FindSetting(options, "engine_renew_months");
	REQUIRE(row.has_value());
	Window *query = OpenNumericQuery(options, *row);
	QueryString *native_text = query->GetQueryString(WID_QS_TEXT);
	REQUIRE(native_text != nullptr);
	const Point ok = Centre(query->GetWidget<NWidgetBase>(WID_QS_OK)->GetCurrentRect());
	const Point defaults = Centre(query->GetWidget<NWidgetBase>(WID_QS_DEFAULT)->GetCurrentRect());

	Window *parent = query->parent;
	query->parent = nullptr;
	CHECK_FALSE(ReplaceText(query, "9"));
	CHECK_FALSE(Click(query, ok));
	CHECK_FALSE(Click(query, defaults));
	query->parent = parent;

	/* Replacing the native editbox mapping changes both QueryString and
	 * Textbuf provenance while preserving the outer dialog and parent. */
	QueryString substitute(32);
	query->querystrings[WID_QS_TEXT] = &substitute;
	CHECK_FALSE(ReplaceText(query, "9"));
	CHECK_FALSE(Click(query, ok));
	CHECK_FALSE(Click(query, defaults));
	query->querystrings[WID_QS_TEXT] = native_text;
	CHECK(std::string(native_text->text.GetText()) != "9");
}

TEST_CASE("Company settings numeric dialog cannot borrow a retargeted or reopened lease", "[integration][company-settings-gui]")
{
	CompanySettingsWindows world;
	RendererRole role;
	world.Open();
	Window *options = world.options;
	REQUIRE(Click(options, Centre(options->GetWidget<NWidgetBase>(WID_GO_TAB_ADVANCED)->GetCurrentRect())));
	const auto first_row = FindSetting(options, "engine_renew_months");
	REQUIRE(first_row.has_value());
	Window *first_query = OpenNumericQuery(options, *first_row);
	const auto first_entry = CurrentGameOptionsValueEntry(options);
	REQUIRE(first_entry.has_value());

	/* A native parent click can change valuewindow_entry without carrying the
	 * renderer's approved source gesture to the replacement dialog. */
	const auto other_row = FindSetting(options, "vehicle.servint_trains");
	REQUIRE(other_row.has_value());
	options->OnClick(*other_row, WID_GO_OPTIONSPANEL, 1);
	options->OnClick(*other_row, WID_GO_OPTIONSPANEL, 1);
	const auto current = CurrentGameOptionsValueEntry(options);
	REQUIRE(current.has_value());
	CHECK(current->entry != first_entry->entry);
	if (Window *dialog = FindWindowById(WindowClass::QueryString, QueryStringWindowNumber::Default); dialog != nullptr) {
		CHECK_FALSE(ReplaceText(dialog, "15"));
		CHECK_FALSE(Click(dialog, Centre(dialog->GetWidget<NWidgetBase>(WID_QS_OK)->GetCurrentRect())));
		CHECK_FALSE(Click(dialog, Centre(dialog->GetWidget<NWidgetBase>(WID_QS_DEFAULT)->GetCurrentRect())));
	}
	/* The old pointer is either closed or stale; it cannot authorize a new
	 * query merely because the native window class and parent still match. */
	CHECK(first_query != nullptr);
}

TEST_CASE("Company settings live dialog rejects changed value-entry descriptor", "[integration][company-settings-gui]")
{
	CompanySettingsWindows world;
	RendererRole role;
	world.Open();
	Window *options = world.options;
	REQUIRE(Click(options, Centre(options->GetWidget<NWidgetBase>(WID_GO_TAB_ADVANCED)->GetCurrentRect())));
	const auto other_row = FindSetting(options, "vehicle.servint_trains");
	REQUIRE(other_row.has_value());
	const IntSettingDesc *other_descriptor = ClassifyGameOptionsHit(options, *other_row).setting.descriptor;
	REQUIRE(other_descriptor != nullptr);
	const auto row = FindSetting(options, "engine_renew_months");
	REQUIRE(row.has_value());
	Window *query = OpenNumericQuery(options, *row);
	const auto selected = CurrentGameOptionsValueEntry(options);
	REQUIRE(selected.has_value());
	REQUIRE(selected->descriptor != other_descriptor);

	/* The dialog, parent, QueryString and Textbuf are unchanged. Only the
	 * selected native row's descriptor moves to another allowed policy. */
	auto *entry = const_cast<SettingEntry *>(selected->entry);
	struct RestoreDescriptor {
		SettingEntry *entry;
		const IntSettingDesc *original;
		~RestoreDescriptor() { entry->setting = original; }
	} restore{entry, entry->setting};
	entry->setting = other_descriptor;
	REQUIRE(query->parent == options);
	REQUIRE(query->GetQueryString(WID_QS_TEXT) != nullptr);
	CHECK_FALSE(ReplaceText(query, "10"));
	CHECK_FALSE(Click(query, Centre(query->GetWidget<NWidgetBase>(WID_QS_OK)->GetCurrentRect())));
	CHECK_FALSE(Click(query, Centre(query->GetWidget<NWidgetBase>(WID_QS_DEFAULT)->GetCurrentRect())));
}

TEST_CASE("Company settings reopened native dialog cannot inherit an old renderer lease", "[integration][company-settings-gui]")
{
	CompanySettingsWindows world;
	RendererRole role;
	world.Open();
	Window *options = world.options;
	REQUIRE(Click(options, Centre(options->GetWidget<NWidgetBase>(WID_GO_TAB_ADVANCED)->GetCurrentRect())));
	const auto row = FindSetting(options, "engine_renew_months");
	REQUIRE(row.has_value());
	Window *old_query = OpenNumericQuery(options, *row);
	old_query->Close();
	Window::DeleteClosedWindows();
	options->OnClick(*row, WID_GO_OPTIONSPANEL, 1);
	options->OnClick(*row, WID_GO_OPTIONSPANEL, 1);
	Window *reopened = FindWindowById(WindowClass::QueryString, QueryStringWindowNumber::Default);
	REQUIRE(reopened != nullptr);
	REQUIRE(reopened->parent == options);
	CHECK_FALSE(ReplaceText(reopened, "11"));
	CHECK_FALSE(Click(reopened, Centre(reopened->GetWidget<NWidgetBase>(WID_QS_OK)->GetCurrentRect())));
	CHECK_FALSE(Click(reopened, Centre(reopened->GetWidget<NWidgetBase>(WID_QS_DEFAULT)->GetCurrentRect())));
}
