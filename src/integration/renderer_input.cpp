/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 * OpenTTD is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
 * See the GNU General Public License for more details. You should have received a copy of the GNU General Public License along with OpenTTD. If not, see <https://www.gnu.org/licenses/old-licenses/gpl-2.0>.
 */

/** @file renderer_input.cpp Allowlisted input through OpenTTD's ordinary GUI. */

#include "../stdafx.h"
#include "renderer_input.h"
#include "actions_internal.h"
#include "actions_story.h"
#include "bridge_hooks.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../core/backup_type.hpp"
#include "../gfx_func.h"
#include "../newgrf_debug.h"
#include "../network/network_base.h"
#include "../widgets/network_widget.h"
#include "../openttd.h"
#include "../querystring_gui.h"
#include "../settings_gui.h"
#include "../settings_internal.h"
#include "../settings_type.h"
#include "../textfile_type.h"
#include "../string_func.h"
#include "../tilehighlight_func.h"
#include "../vehicle_base.h"
#include "../viewport_func.h"
#include "../window_func.h"
#include "../window_gui.h"
#include "../widgets/toolbar_widget.h"
#include "../widgets/company_widget.h"
#include "../widgets/autoreplace_widget.h"
#include "../widgets/depot_widget.h"
#include "../widgets/date_widget.h"
#include "../widgets/engine_widget.h"
#include "../widgets/group_widget.h"
#include "../widgets/goal_widget.h"
#include "../widgets/misc_widget.h"
#include "../widgets/order_widget.h"
#include "../widgets/settings_widget.h"
#include "../widgets/station_widget.h"
#include "../widgets/sign_widget.h"
#include "../widgets/story_widget.h"
#include "../widgets/timetable_widget.h"
#include "../widgets/town_widget.h"
#include "../widgets/vehicle_widget.h"
#include "../widgets/waypoint_widget.h"

#include "../safeguards.h"

namespace Integration {
namespace {

/* A toolbar dropdown does not expose its originating widget through Window.
 * Remember only the exact live dropdown created by our approved toolbar click. */
const Window *_toolbar_dropdown = nullptr;
const Window *_toolbar_dropdown_parent = nullptr;
WidgetID _toolbar_dropdown_widget = INVALID_WIDGET;

struct InputOrigin {
	const Window *window = nullptr;
	WindowClass window_class{};
	WindowNumber number = 0;
	WidgetID widget = INVALID_WIDGET;
};

struct QueryOrigin {
	const Window *dialog = nullptr;
	const QueryString *query = nullptr;
	InputOrigin source;
	const Textbuf *textbuf = nullptr;
	GameOptionsSettingState setting{};
};

struct CompanySettingGesture {
	InputOrigin source;
	Point local{};
	GameOptionsSettingState setting{};
	bool via_query = false;
};
CompanySettingGesture _company_setting_gesture;

struct ModalOrigin {
	const Window *dialog = nullptr;
	InputOrigin source;
	uint64_t vehicle_generation = 0;
};

struct StoryPlacementOrigin {
	InputOrigin source;
	StoryPageElementID element;
	std::string page_version;
};

struct ClientListAccessOrigin {
	const Window *window = nullptr;
	std::string image_id;
	std::string version;
	const Window *policy_dropdown = nullptr;
	std::optional<ClientListAccessTarget> armed;
	bool armed_policy = false; ///< Survives dropdown close until the gesture's RAII return.
};
ClientListAccessOrigin _access_origin;

QueryOrigin _query_origin;
std::vector<ModalOrigin> _confirm_origins;
ModalOrigin _date_origin;
StoryPlacementOrigin _story_placement;
InputOrigin _question_origin;
InputOrigin _last_input_origin;
Commands _callback_command = Commands::End;
const Window *_callback_previous_query = nullptr;

bool IsLiveWindow(const Window *window)
{
	if (window == nullptr) return false;
	for (const Window *candidate : Window::Iterate()) {
		if (candidate == window) return true;
	}
	return false;
}

bool IsLiveOrigin(const InputOrigin &origin)
{
	return IsLiveWindow(origin.window) && origin.window->window_class == origin.window_class && origin.window->window_number == origin.number;
}

bool IsAllowedCompanySettingRow(GameOptionsSettingState state)
{
	return state.entry != nullptr && state.descriptor != nullptr &&
		Detail::IsAllowedCompanySetting(state.descriptor) &&
		!state.descriptor->flags.Test(SettingFlag::GuiDropdown);
}

bool AccessSnapshotCurrent()
{
	return IsLiveWindow(_access_origin.window) && _access_origin.window->window_class == WindowClass::NetworkClientList &&
		!_access_origin.image_id.empty() && !_access_origin.version.empty() &&
		NetworkClientListAccessVersion(_access_origin.window) == _access_origin.version;
}

bool AccessPolicyDropdown(const Window *window)
{
	return window != nullptr && window == _access_origin.policy_dropdown && IsLiveWindow(window) &&
		window->window_class == WindowClass::DropdownMenu && window->parent == _access_origin.window && AccessSnapshotCurrent();
}

struct AccessGestureRelease {
	~AccessGestureRelease() { _access_origin.armed.reset(); _access_origin.armed_policy = false; }
};

bool ValidateStoryPlacement()
{
	Window *callback = _thd.GetCallbackWnd();
	if (callback == nullptr || callback->window_class != WindowClass::StoryBook) {
		_story_placement = {};
		return true;
	}
	try {
		if (IsLiveOrigin(_story_placement.source) && _story_placement.source.window == callback &&
				_story_placement.source.window_class == WindowClass::StoryBook && _story_placement.source.widget == WID_SB_PAGE_PANEL &&
				RendererStoryPlacementElement(callback) == _story_placement.element &&
				Detail::StoryPlacementVersion(_story_placement.element) == _story_placement.page_version) return true;
	} catch (...) {
		/* Changed/deleted script context must cancel the native tool as well. */
	}
	_story_placement = {};
	ResetObjectToPlace();
	return false;
}

bool RememberStoryPlacement(const InputOrigin &origin, bool may_arm)
{
	Window *callback = _thd.GetCallbackWnd();
	if (callback == nullptr || callback->window_class != WindowClass::StoryBook) {
		_story_placement = {};
		return true;
	}
	StoryPageElementID element = RendererStoryPlacementElement(callback);
	if (element == StoryPageElementID::Invalid()) return false;
	std::string version = Detail::StoryPlacementVersion(element);
	if (IsLiveOrigin(_story_placement.source) && _story_placement.source.window == callback &&
			_story_placement.element == element && _story_placement.page_version == version) return true;
	if (!may_arm || !IsLiveOrigin(origin) || origin.window != callback || origin.window_class != WindowClass::StoryBook || origin.widget != WID_SB_PAGE_PANEL) return false;
	_story_placement = {origin, element, std::move(version)};
	return true;
}

bool IsGroupWindow(WindowClass window_class)
{
	return window_class == WindowClass::TrainList || window_class == WindowClass::RoadVehicleList ||
		window_class == WindowClass::ShipList || window_class == WindowClass::AircraftList;
}

bool IsAllowedQueryOrigin(const InputOrigin &origin)
{
	if (!IsLiveOrigin(origin)) return false;
	switch (origin.window_class) {
		case WindowClass::VehicleView: return origin.widget == WID_VV_RENAME;
		case WindowClass::VehicleDepot: return origin.widget == WID_D_RENAME;
		case WindowClass::StationView: return origin.widget == WID_SV_RENAME;
		case WindowClass::WaypointView: return origin.widget == WID_W_RENAME;
		case WindowClass::VehicleOrders: return origin.widget == WID_O_COND_VALUE;
		case WindowClass::VehicleTimetable:
			return origin.widget == WID_VT_START_DATE || origin.widget == WID_VT_CHANGE_TIME || origin.widget == WID_VT_CHANGE_SPEED;
		case WindowClass::Company:
			if (origin.window->window_number == _local_company) return origin.widget == WID_C_COMPANY_NAME || origin.widget == WID_C_PRESIDENT_NAME;
			return origin.widget == WID_C_GIVE_MONEY;
		case WindowClass::CompanyManagerFace:
			return origin.window->window_number == _local_company && origin.widget == WID_SCMF_FACECODE;
		default:
			return IsGroupWindow(origin.window_class) && origin.widget == WID_GL_RENAME_GROUP;
	}
}

bool IsAllowedQuery(const Window *window)
{
	if (!(window == _query_origin.dialog && IsLiveWindow(window) &&
		window->window_class == WindowClass::QueryString && window->window_number == QueryStringWindowNumber::Default &&
		IsLiveOrigin(_query_origin.source) && window->parent == _query_origin.source.window &&
		window->GetQueryString(WID_QS_TEXT) == _query_origin.query)) return false;
	if (_query_origin.source.window_class != WindowClass::GameOptions) return true;
	if (_query_origin.textbuf != &_query_origin.query->text || _query_origin.source.widget != WID_GO_OPTIONSPANEL ||
		!IsAllowedCompanySettingRow(_query_origin.setting)) return false;
	auto current = CurrentGameOptionsValueEntry(_query_origin.source.window);
	return current.has_value() && current->entry == _query_origin.setting.entry &&
		current->descriptor == _query_origin.setting.descriptor;
}

void RememberQuery(const InputOrigin &origin, const Window *previous, bool callback = false,
		std::optional<GameOptionsSettingState> pre_setting = std::nullopt)
{
	if (!IsLiveOrigin(origin)) return;
	if (origin.window_class == WindowClass::GameOptions) {
		if (callback || origin.widget != WID_GO_OPTIONSPANEL || !pre_setting.has_value() ||
			!IsAllowedCompanySettingRow(*pre_setting)) return;
		auto current = CurrentGameOptionsValueEntry(origin.window);
		if (!current.has_value() || current->entry != pre_setting->entry || current->descriptor != pre_setting->descriptor) return;
	} else if (!callback && !IsAllowedQueryOrigin(origin)) {
		return;
	}
	Window *query = FindWindowById(WindowClass::QueryString, QueryStringWindowNumber::Default);
	if (query == nullptr || query == previous || query->parent != origin.window || query->GetQueryString(WID_QS_TEXT) == nullptr) return;
	QueryString *text = query->GetQueryString(WID_QS_TEXT);
	_query_origin = {query, text, origin};
	if (origin.window_class == WindowClass::GameOptions) {
		_query_origin.textbuf = &text->text;
		_query_origin.setting = *pre_setting;
	}
}

bool IsAllowedConfirmation(const Window *window)
{
	if (!IsLiveWindow(window) || window->window_class != WindowClass::ConfirmPopupQuery || window->window_number != ConfirmPopupQueryWindowNumber::Default) return false;
	return std::ranges::any_of(_confirm_origins, [window](const ModalOrigin &origin) {
		return origin.dialog == window && IsLiveOrigin(origin.source) && window->parent == origin.source.window &&
			IsGroupWindow(origin.source.window_class) && origin.source.widget == WID_GL_DELETE_GROUP && origin.source.window->owner == _local_company;
	});
}

bool IsAllowedDate(const Window *window)
{
	if (window != _date_origin.dialog || !IsLiveWindow(window) || window->window_class != WindowClass::SetDate || !IsLiveOrigin(_date_origin.source)) return false;
	const InputOrigin &source = _date_origin.source;
	if (window->parent != source.window || source.window_class != WindowClass::VehicleTimetable || source.widget != WID_VT_START_DATE || window->window_number != source.number) return false;
	const Vehicle *vehicle = Vehicle::GetIfValid(VehicleID(source.number));
	return vehicle != nullptr && vehicle->owner == _local_company && vehicle->GetInstanceGeneration() == _date_origin.vehicle_generation;
}

void RememberModal(const InputOrigin &origin, const Window *previous_confirm, const Window *previous_date)
{
	if (!IsLiveOrigin(origin) || origin.window->owner != _local_company) return;
	if (IsGroupWindow(origin.window_class) && origin.widget == WID_GL_DELETE_GROUP) {
		/* A default-number confirmation can exist for more than one parent. */
		Window *dialog = origin.window->FindChildWindowById(WindowClass::ConfirmPopupQuery, ConfirmPopupQueryWindowNumber::Default);
		if (dialog == nullptr || dialog == previous_confirm || dialog->parent != origin.window) return;
		std::erase_if(_confirm_origins, [](const ModalOrigin &item) { return !IsLiveWindow(item.dialog) || !IsLiveOrigin(item.source); });
		if (_confirm_origins.size() < 8) _confirm_origins.push_back({dialog, origin, 0});
	} else if (origin.window_class == WindowClass::VehicleTimetable && origin.widget == WID_VT_START_DATE) {
		Window *dialog = origin.window->FindChildWindowById(WindowClass::SetDate, origin.number);
		const Vehicle *vehicle = Vehicle::GetIfValid(VehicleID(origin.number));
		if (dialog != nullptr && dialog != previous_date && dialog->parent == origin.window && vehicle != nullptr && vehicle->owner == _local_company) {
			_date_origin = {dialog, origin, vehicle->GetInstanceGeneration()};
		}
	}
}

bool IsAllowedToolbarWidget(WidgetID widget)
{
	switch (widget) {
		case WID_TN_SMALL_MAP:
		case WID_TN_SETTINGS:
		case WID_TN_HELP:
		case WID_TN_MESSAGES:
		case WID_TN_TOWNS:
		case WID_TN_SUBSIDIES:
		case WID_TN_STATIONS:
		case WID_TN_FINANCES:
		case WID_TN_COMPANIES: // Native membership moves are separately blocked at NetworkClientRequestMove.
		case WID_TN_STORY:
		case WID_TN_GOAL:
		case WID_TN_GRAPHS:
		case WID_TN_LEAGUE:
		case WID_TN_INDUSTRIES:
		case WID_TN_TRAINS:
		case WID_TN_ROADVEHS:
		case WID_TN_SHIPS:
		case WID_TN_AIRCRAFT:
		case WID_TN_ZOOM_IN:
		case WID_TN_ZOOM_OUT:
		case WID_TN_RAILS:
		case WID_TN_ROADS:
		case WID_TN_TRAMS:
		case WID_TN_WATER:
		case WID_TN_AIR:
		case WID_TN_LANDSCAPE:
		case WID_TN_SWITCH_BAR:
			return true;
		default:
			return false;
	}
}

bool IsAllowedWindowClass(WindowClass window_class)
{
	switch (window_class) {
		case WindowClass::MainWindow:
		case WindowClass::BuildToolbar:
		case WindowClass::BuildTrees:
		case WindowClass::BuildSignal:
		case WindowClass::BuildVehicle:
		case WindowClass::BuildBridge:
		case WindowClass::BuildObject:
		case WindowClass::BuildHouse:
		case WindowClass::FoundTown:
		case WindowClass::BuildIndustry:
		case WindowClass::BuildStation:
		case WindowClass::BuildBusStation:
		case WindowClass::BuildTruckStation:
		case WindowClass::BuildDepot:
		case WindowClass::BuildWaypoint:
		case WindowClass::ScenarioGenerateLandscape: // The normal-game terraform toolbar uses this class.
		case WindowClass::JoinStation:
		case WindowClass::VehicleDepot:
		case WindowClass::VehicleView:
		case WindowClass::VehicleDetails:
		case WindowClass::VehicleOrders:
		case WindowClass::VehicleRefit:
		case WindowClass::VehicleTimetable:
		case WindowClass::ReplaceVehicle:
		case WindowClass::StationList:
		case WindowClass::TrainList:
		case WindowClass::RoadVehicleList:
		case WindowClass::ShipList:
		case WindowClass::AircraftList:
		case WindowClass::StationView:
		case WindowClass::WaypointView:
		case WindowClass::TownView:
		case WindowClass::TownAuthority:
		case WindowClass::TownDirectory:
		case WindowClass::TownCargoGraph:
		case WindowClass::IndustryView:
		case WindowClass::IndustryDirectory:
		case WindowClass::IndustryCargoes:
		case WindowClass::IndustryProductionGraph:
		case WindowClass::SubsidyList:
		case WindowClass::SmallMap:
		case WindowClass::ExtraViewport:
		case WindowClass::LandInfo:
		case WindowClass::Finances:
		case WindowClass::Company:
		case WindowClass::CompanyLivery:
		case WindowClass::CompanyManagerFace:
		case WindowClass::CompanyInfrastructure:
		case WindowClass::IncomeGraph:
		case WindowClass::OperatingProfitGraph:
		case WindowClass::DeliveredCargoGraph:
		case WindowClass::PerformanceGraph:
		case WindowClass::CompanyValueGraph:
		case WindowClass::CompanyLeague:
		case WindowClass::CargoPaymentRatesGraph:
		case WindowClass::PerformanceDetail:
		case WindowClass::GraphLegend:
		case WindowClass::LinkGraphLegend:
		case WindowClass::SignList:
		case WindowClass::News:
		case WindowClass::MessageHistory:
		case WindowClass::StoryBook:
		case WindowClass::GoalList:
		case WindowClass::GoalQuestion:
		case WindowClass::ErrorMessage:
			return true;
		default:
			/* Includes settings, saves, network, console,
			 * queries, confirmations, keyboards, scripts and debug windows. */
			return false;
	}
}

bool IsAllowedCompanyWidget(const Window *window, const NWidgetCore *widget)
{
	/* Company membership bypasses gameplay commands entirely. Never allow it,
	 * even if the native button is visible and enabled for this socket. */
	if (widget->GetIndex() == WID_C_COMPANY_JOIN) return false;
	if (widget->GetIndex() == INVALID_WIDGET) return true; // Native window frame controls.
	switch (widget->GetIndex()) {
		case WID_C_NEW_FACE:
		case WID_C_COLOUR_SCHEME:
		case WID_C_PRESIDENT_NAME:
		case WID_C_COMPANY_NAME:
		case WID_C_BUILD_HQ:
		case WID_C_RELOCATE_HQ:
			return window->window_number == _local_company;
		case WID_C_GIVE_MONEY:
		case WID_C_HOSTILE_TAKEOVER:
			return window->window_number != _local_company;
		case WID_C_CAPTION:
		case WID_C_FACE:
		case WID_C_FACE_TITLE:
		case WID_C_DESC_INAUGURATION:
		case WID_C_DESC_COLOUR_SCHEME:
		case WID_C_DESC_COLOUR_SCHEME_EXAMPLE:
		case WID_C_DESC_VEHICLE:
		case WID_C_DESC_VEHICLE_COUNTS:
		case WID_C_DESC_COMPANY_VALUE:
		case WID_C_DESC_INFRASTRUCTURE:
		case WID_C_DESC_INFRASTRUCTURE_COUNTS:
		case WID_C_VIEW_HQ:
		case WID_C_VIEW_INFRASTRUCTURE:
			return true;
		default:
			return false;
	}
}

bool IsAllowedInteractiveWindow(const Window *window)
{
	if (!IsLiveWindow(window)) return false;
	if (window->window_class == WindowClass::GameOptions) return window->window_number == to_underlying(GameOptionsWindowNumber::GameOptions) && !window->IsShaded();
	if (window->window_class == WindowClass::QueryString) return IsAllowedQuery(window) || RendererSignEditorEditable(window);
	if (window->window_class == WindowClass::ConfirmPopupQuery) return IsAllowedConfirmation(window);
	if (window->window_class == WindowClass::SetDate) return IsAllowedDate(window);
	if (window->window_class == WindowClass::BuyCompany || window->window_class == WindowClass::EnginePreview) return true;
	if (window->window_class == WindowClass::NetworkClientList) return true;
	if (window->window_class == WindowClass::CompanyLivery || window->window_class == WindowClass::CompanyManagerFace) return window->window_number == _local_company;
	return IsAllowedWindowClass(window->window_class);
}

bool IsAllowedDropdown(const Window *window)
{
	if (!IsLiveWindow(window) || window->window_class != WindowClass::DropdownMenu || !IsLiveWindow(window->parent)) return false;
	if (window->parent->window_class == WindowClass::GameOptions) return false;
	if (window->parent->window_class == WindowClass::NetworkClientList) return AccessPolicyDropdown(window);
	if (window->parent->window_class != WindowClass::MainToolbar) return IsAllowedInteractiveWindow(window->parent);
	return window == _toolbar_dropdown && window->parent == _toolbar_dropdown_parent && IsAllowedToolbarWidget(_toolbar_dropdown_widget);
}

bool IsAllowedTarget(const Window *window, int x, int y, bool read_only = false)
{
	if (window == nullptr || window->nested_root == nullptr) return false;
	const NWidgetCore *widget = window->nested_root->GetWidgetFromPos(x - window->left, y - window->top);
	if (widget == nullptr || widget->IsDisabled()) return false;
	/* Edit boxes can open the on-screen keyboard. Debug boxes bypass the
	 * owning window's OnClick and open an inspector directly. */
	if ((widget->type & WWT_MASK) == WWT_EDITBOX || (widget->type & WWT_MASK) == WWT_DEBUGBOX) return false;
	if (window->window_class == WindowClass::MainToolbar) {
		return IsAllowedToolbarWidget(widget->GetIndex());
	}
	if (window->window_class == WindowClass::GameOptions) {
		GameOptionsHit hit = ClassifyGameOptionsHit(window, {x - window->left, y - window->top});
		return hit.kind == GameOptionsHitKind::Navigation ||
			(hit.kind == GameOptionsHitKind::Setting && IsAllowedCompanySettingRow(hit.setting));
	}
	if (window->window_class == WindowClass::Company) return IsAllowedCompanyWidget(window, widget);
	if (window->window_class == WindowClass::NetworkClientList) {
		if (!AccessSnapshotCurrent() || window != _access_origin.window) return false;
		switch (widget->GetIndex()) {
			case INVALID_WIDGET:
			case WID_CL_SCROLLBAR:
			case WID_CL_SERVER_NAME:
			case WID_CL_CLIENT_NAME:
			case WID_CL_CLIENT_COMPANY_COUNT: return true;
			case WID_CL_MATRIX: return read_only || NetworkClientListAccessTargetAt(window, {x - window->left, y - window->top}).has_value();
			default: return false;
		}
	}
	if (window->window_class == WindowClass::StoryBook) {
		switch (widget->GetIndex()) {
			case INVALID_WIDGET:
			case WID_SB_CAPTION:
			case WID_SB_SEL_PAGE:
			case WID_SB_PAGE_PANEL:
			case WID_SB_SCROLLBAR:
			case WID_SB_PREV_PAGE:
			case WID_SB_NEXT_PAGE: return true;
			default: return false;
		}
	}
	if (window->window_class == WindowClass::GoalList) {
		switch (widget->GetIndex()) {
			case INVALID_WIDGET:
			case WID_GOAL_CAPTION:
			case WID_GOAL_SELECT_BUTTONS:
			case WID_GOAL_GLOBAL_BUTTON:
			case WID_GOAL_COMPANY_BUTTON:
			case WID_GOAL_LIST:
			case WID_GOAL_SCROLLBAR: return true;
			default: return false;
		}
	}
	if (window->window_class == WindowClass::GoalQuestion) {
		switch (widget->GetIndex()) {
			case INVALID_WIDGET:
			case WID_GQ_CAPTION:
			case WID_GQ_QUESTION:
			case WID_GQ_BUTTON_1:
			case WID_GQ_BUTTON_2:
			case WID_GQ_BUTTON_3: return true;
			default: return false;
		}
	}
	if (window->window_class == WindowClass::ConfirmPopupQuery) {
		return IsAllowedConfirmation(window) && (widget->GetIndex() == INVALID_WIDGET || widget->GetIndex() == WID_Q_CAPTION || widget->GetIndex() == WID_Q_YES || widget->GetIndex() == WID_Q_NO);
	}
	if (window->window_class == WindowClass::SetDate) {
		return IsAllowedDate(window) && (widget->GetIndex() == INVALID_WIDGET || widget->GetIndex() == WID_SD_DAY || widget->GetIndex() == WID_SD_MONTH || widget->GetIndex() == WID_SD_YEAR || widget->GetIndex() == WID_SD_SET_DATE);
	}
	if (window->window_class == WindowClass::BuyCompany) {
		if (widget->GetIndex() == WID_BC_YES) {
			if (window->window_number >= MAX_COMPANIES || window->window_number == _local_company) return false;
			const Company *company = Company::GetIfValid(CompanyID(static_cast<uint8_t>(window->window_number)));
			return company != nullptr && company->bankrupt_asked.Test(_local_company);
		}
		return widget->GetIndex() == INVALID_WIDGET || widget->GetIndex() == WID_BC_CAPTION || widget->GetIndex() == WID_BC_NO || widget->GetIndex() == WID_BC_FACE || widget->GetIndex() == WID_BC_QUESTION;
	}
	if (window->window_class == WindowClass::EnginePreview) {
		switch (widget->GetIndex()) {
			case INVALID_WIDGET:
			case WID_EP_CAPTION:
			case WID_EP_QUESTION:
			case WID_EP_NO:
			case WID_EP_YES:
			case WID_EP_PREV:
			case WID_EP_LIST:
			case WID_EP_NEXT:
				return true;
			default:
				return false;
		}
	}
	if (window->window_class == WindowClass::QueryString) {
		if (RendererSignEditorEditable(window)) {
			switch (widget->GetIndex()) {
				case INVALID_WIDGET:
				case WID_QES_CAPTION:
				case WID_QES_LOCATION:
				case WID_QES_OK:
				case WID_QES_CANCEL:
				case WID_QES_DELETE:
				case WID_QES_MOVE:
				case WID_QES_PREVIOUS:
				case WID_QES_NEXT: return true;
				default: return false;
			}
		}
		if (!IsAllowedQuery(window)) return false;
		switch (widget->GetIndex()) {
			case INVALID_WIDGET:
			case WID_QS_CAPTION:
			case WID_QS_OK:
			case WID_QS_CANCEL:
			case WID_QS_DEFAULT:
				return true;
			case WID_QS_MOVE:
				return _query_origin.source.window_class != WindowClass::GameOptions;
			default:
				return false;
		}
	}
	if (window->window_class == WindowClass::DropdownMenu) {
		if (!IsAllowedDropdown(window)) return false;
	} else if (!IsAllowedInteractiveWindow(window)) {
		return false;
	}
	return true;
}

bool ReadCoordinate(const nlohmann::json &params, const char *key, int limit, int &value)
{
	const auto field = params.find(key);
	if (field == params.end() || !field->is_number_integer() || limit <= 0) return false;
	if (field->is_number_unsigned()) {
		const uint64_t number = field->get<uint64_t>();
		if (number >= static_cast<uint64_t>(limit)) return false;
		value = static_cast<int>(number);
	} else {
		const int64_t number = field->get<int64_t>();
		if (number < 0 || number >= limit) return false;
		value = static_cast<int>(number);
	}
	return true;
}

void ClearButtons(bool ctrl = false)
{
	_left_button_down = false;
	_left_button_clicked = false;
	_right_button_down = false;
	_right_button_clicked = false;
	_ctrl_pressed = ctrl;
	_shift_pressed = false;
	_dirkeys = {};
	_cursor.fix_at = false;
	_cursor.wheel = 0;
	_cursor.wheel_moved = false;
	_cursor.h_wheel = 0;
	_cursor.v_wheel = 0;
	_cursor.delta = {0, 0};
}

struct InputRelease {
	bool completed = false;

	~InputRelease()
	{
		_company_setting_gesture = {};
		ClearButtons();
		if (this->completed) return;
		/* Cancel placement before releasing after a failed handler: a delayed
		 * placement must never escape the caller's command-capture scope. */
		/* OnFocusLost(false) disables InstantClose before closing a native
		 * dropdown, cancelling a selection that has not completed yet. */
		for (uint i = 0; i < 8; ++i) {
			Window *dropdown = FindWindowById(WindowClass::DropdownMenu, 0);
			if (dropdown == nullptr) break;
			try {
				dropdown->OnFocusLost(false);
			} catch (...) {
				/* Continue releasing the remaining native input state. */
			}
			if (IsLiveWindow(dropdown)) break;
		}
		_toolbar_dropdown = nullptr;
		_toolbar_dropdown_parent = nullptr;
		_toolbar_dropdown_widget = INVALID_WIDGET;
		_story_placement = {};
		try {
			ResetObjectToPlace();
		} catch (...) {
			/* Still dispatch the button release if placement cleanup failed. */
		}
		try {
			HandleMouseEvents();
		} catch (...) {
			/* Button state must remain released even if native cleanup fails. */
		}
		ClearButtons();
	}
};

struct QuestionGestureRelease {
	~QuestionGestureRelease() { _question_origin = {}; }
};

struct CompanySettingGestureRelease {
	~CompanySettingGestureRelease() { _company_setting_gesture = {}; }
};

bool InputIsIdle()
{
	if (_ctrl_pressed || _shift_pressed || _dirkeys.Any() || _newgrf_debug_sprite_picker.mode != SPM_NONE) return false;
	if (_left_button_down || _left_button_clicked || _right_button_down || _right_button_clicked || _cursor.fix_at || _scrolling_viewport) return false;
	if (_special_mouse_mode != SpecialMouseMode::None && _special_mouse_mode != SpecialMouseMode::Presize) return false;
	if (Window *callback = _thd.GetCallbackWnd(); callback != nullptr && !IsAllowedInteractiveWindow(callback) && callback->window_class != WindowClass::MainToolbar) return false;
	for (const Window *window : Window::Iterate()) {
		if (window->mouse_capture_widget != INVALID_WIDGET || window->flags.Any({WindowFlag::Dragging, WindowFlag::SizingLeft, WindowFlag::SizingRight})) return false;
		/* Focus loss can commit an InstantClose dropdown. Reject unsafe menus
		 * even when this gesture targets another window. */
		if (window->window_class == WindowClass::DropdownMenu && !IsAllowedDropdown(window)) return false;
	}
	return true;
}

void RememberToolbarDropdown(const Window *parent, WidgetID widget, const Window *previous)
{
	if (!IsLiveWindow(parent) || parent->window_class != WindowClass::MainToolbar || !IsAllowedToolbarWidget(widget)) return;
	Window *dropdown = FindWindowById(WindowClass::DropdownMenu, 0);
	if (dropdown == nullptr || dropdown == previous || dropdown->parent != parent) return;
	_toolbar_dropdown = dropdown;
	_toolbar_dropdown_parent = parent;
	_toolbar_dropdown_widget = widget;
}

void RememberAccessDropdown(const Window *parent, const Window *previous)
{
	if (!_access_origin.armed || _access_origin.armed->kind != ClientListAccessKind::OwnPolicyMenu || parent != _access_origin.window) return;
	Window *dropdown = FindWindowById(WindowClass::DropdownMenu, 0);
	if (dropdown != nullptr && dropdown != previous && dropdown->parent == parent) _access_origin.policy_dropdown = dropdown;
}

void DrainDropdown(Window *dropdown)
{
	/* Selection normally waits four 30 ms mouse loops. Run only this approved
	 * control's normal handler so its callback remains inside command capture.
	 * The initial drag-release transition can use one further pass. */
	for (uint i = 0; i < 5 && IsAllowedDropdown(dropdown); ++i) dropdown->OnMouseLoop();
}

bool ApplyQueryText(Window *window, int x, int y, const nlohmann::json &params)
{
	if (window == nullptr || window->nested_root == nullptr || !params.contains("text") || !params.at("text").is_string()) return false;
	WidgetID editor = WID_QS_TEXT;
	QueryString *query = nullptr;
	if (IsAllowedQuery(window)) query = window->GetQueryString(editor);
	else if (RendererSignEditorEditable(window)) {
		editor = WID_QES_TEXT;
		query = window->GetQueryString(editor);
	} else {
		query = RendererFoundTownNameQuery(window);
		editor = WID_TF_TOWN_NAME_EDITBOX;
	}
	if (query == nullptr) return false;
	const NWidgetCore *widget = window->nested_root->GetWidgetFromPos(x - window->left, y - window->top);
	if (widget == nullptr || widget->IsDisabled() || widget->GetIndex() != editor || (widget->type & WWT_MASK) != WWT_EDITBOX) return false;
	const std::string &text = params.at("text").get_ref<const std::string &>();
	if (text.size() > 4096 || StrMakeValid(text) != text) return false;
	/* Validate through the same native filter and size limits without changing
	 * the live buffer. InsertString deliberately truncates invalid input, so an
	 * exact roundtrip is required before accepting the replacement. */
	Textbuf candidate(query->text.max_bytes, query->text.max_chars);
	candidate.afilter = query->text.afilter;
	if (!text.empty() && !candidate.InsertString(text, false)) return false;
	if (candidate.GetText() != text) return false;
	const std::string original(query->text.GetText());
	query->text.DeleteAll();
	if (!text.empty() && !query->text.InsertString(text, false)) {
		query->text.Assign(original);
		return false;
	}
	if (query->text.GetText() != text) {
		query->text.Assign(original);
		return false;
	}
	window->SetWidgetDirty(editor);
	window->OnEditboxChanged(editor);
	return true;
}

} // namespace

void RendererRememberAccessSnapshot(std::string_view image_id)
{
	Window *window = FindWindowById(WindowClass::NetworkClientList, 0);
	if (window == nullptr) { _access_origin = {}; return; }
	if (_access_origin.window != window) _access_origin = {};
	_access_origin.window = window;
	_access_origin.image_id = image_id;
	_access_origin.version = NetworkClientListAccessVersion(window);
}

void RendererClearAccessSnapshot()
{
	_access_origin.image_id.clear(); _access_origin.version.clear();
}

bool RendererAccessCommandMatches(CompanyAllowListCtrlAction action, std::string_view key)
{
	if (action == CompanyAllowListCtrlAction::AllowAny || action == CompanyAllowListCtrlAction::AllowListed)
		return _access_origin.armed_policy && key.empty() && _current_company == _local_company && AccessSnapshotCurrent();
	if (!_access_origin.armed || !AccessSnapshotCurrent()) return false;
	const auto &target = *_access_origin.armed;
	if (target.company != _local_company || _current_company != _local_company || target.key != key) return false;
	if ((action == CompanyAllowListCtrlAction::AddKey && target.kind != ClientListAccessKind::Authorize) ||
		(action == CompanyAllowListCtrlAction::RemoveKey && target.kind != ClientListAccessKind::Revoke)) return false;
	if (action != CompanyAllowListCtrlAction::AddKey && action != CompanyAllowListCtrlAction::RemoveKey) return false;
	const auto *info = NetworkClientInfo::GetByClientID(target.client);
	return info != nullptr && info->GetInstanceGeneration() == target.local_generation && info->public_key == target.key;
}

bool RendererCompanySettingCommandMatches(std::string_view native_name)
{
	if (!RendererIsActive() || _current_company != _local_company || !IsLiveOrigin(_company_setting_gesture.source)) return false;
	const auto &witness = _company_setting_gesture;
	if (witness.source.window_class == WindowClass::GameOptions) {
		if (witness.source.widget != WID_GO_OPTIONSPANEL || !IsAllowedCompanySettingRow(witness.setting) ||
				witness.setting.descriptor->GetName() != native_name) return false;
		if (witness.via_query) {
			return IsAllowedQuery(_query_origin.dialog) && _query_origin.source.window == witness.source.window &&
				_query_origin.setting.entry == witness.setting.entry && _query_origin.setting.descriptor == witness.setting.descriptor;
		}
		GameOptionsHit hit = ClassifyGameOptionsHit(witness.source.window, witness.local);
		return hit.kind == GameOptionsHitKind::Setting && hit.setting.entry == witness.setting.entry &&
			hit.setting.descriptor == witness.setting.descriptor;
	}
	/* The ordinary ReplaceVehicle train toggle uses the prefixed spelling only
	 * when no group is selected. A selected group posts SetGroupFlag instead. */
	return native_name == "company.renew_keep_length" && witness.source.window_class == WindowClass::ReplaceVehicle &&
		witness.source.widget == WID_RV_TRAIN_WAGONREMOVE_TOGGLE && witness.source.number == to_underlying(VehicleType::Train) &&
		witness.source.window->owner == _local_company && !witness.source.window->IsShaded();
}

std::optional<nlohmann::json> RendererQuestionCommandWitness(uint16_t id, GoalQuestionButton button)
{
	if (!RendererIsActive() || !IsLiveOrigin(_question_origin) || _question_origin.window_class != WindowClass::GoalQuestion) return std::nullopt;
	return RendererGoalQuestionWindowWitness(_question_origin.window, _question_origin.widget, id, button);
}

void RendererResetInputProvenance()
{
	_access_origin = {};
	_query_origin = {};
	_company_setting_gesture = {};
	_confirm_origins.clear();
	_date_origin = {};
	_story_placement = {};
	_question_origin = {};
	_last_input_origin = {};
	_callback_command = Commands::End;
	_callback_previous_query = nullptr;
	_toolbar_dropdown = nullptr;
	_toolbar_dropdown_parent = nullptr;
	_toolbar_dropdown_widget = INVALID_WIDGET;
}

void RendererInputWindowClosed(const Window *window)
{
	if (!RendererIsActive()) return;
	if (_access_origin.window == window) _access_origin = {};
	else if (_access_origin.policy_dropdown == window) _access_origin.policy_dropdown = nullptr; // Preserve gesture-local armed_policy for the pending native callback.
	if (_query_origin.dialog == window || _query_origin.source.window == window) _query_origin = {};
	if (_company_setting_gesture.source.window == window) _company_setting_gesture = {};
	std::erase_if(_confirm_origins, [window](const ModalOrigin &origin) { return origin.dialog == window || origin.source.window == window; });
	if (_date_origin.dialog == window || _date_origin.source.window == window) _date_origin = {};
	if (_story_placement.source.window == window) _story_placement = {};
	if (_question_origin.window == window) _question_origin = {};
	if (_last_input_origin.window == window) _last_input_origin = {};
	if (_callback_previous_query == window) _callback_previous_query = nullptr;
	if (_toolbar_dropdown == window || _toolbar_dropdown_parent == window) {
		_toolbar_dropdown = nullptr;
		_toolbar_dropdown_parent = nullptr;
		_toolbar_dropdown_widget = INVALID_WIDGET;
	}
}

void RendererBeginInputCallback(Commands command)
{
	_callback_command = Commands::End;
	_callback_previous_query = nullptr;
	if (!IsLiveOrigin(_last_input_origin) || !IsGroupWindow(_last_input_origin.window_class) || _last_input_origin.window->owner != _local_company) return;
	/* These existing native callbacks only open the created group's rename
	 * dialog. No other owner callback can grant generic query permission. */
	if (command == Commands::CreateGroup && _last_input_origin.widget != WID_GL_CREATE_GROUP) return;
	if (command == Commands::AddVehicleToGroup && _last_input_origin.widget != WID_GL_LIST_VEHICLE && _last_input_origin.widget != WID_GL_LIST_GROUP) return;
	if (command != Commands::CreateGroup && command != Commands::AddVehicleToGroup) return;
	_callback_command = command;
	_callback_previous_query = FindWindowById(WindowClass::QueryString, QueryStringWindowNumber::Default);
}

void RendererEndInputCallback()
{
	if (_callback_command != Commands::End) RememberQuery(_last_input_origin, _callback_previous_query, true);
	_callback_command = Commands::End;
	_callback_previous_query = nullptr;
}

bool RendererApplyInput(const nlohmann::json &params)
{
	_question_origin = {};
	QuestionGestureRelease question_release;
	_company_setting_gesture = {};
	CompanySettingGestureRelease setting_release;
	_access_origin.armed.reset(); _access_origin.armed_policy = false;
	AccessGestureRelease access_release;
	if (!RendererIsActive() || _game_mode != GameMode::Normal || !IsLocalCompany() || _screen.dst_ptr == nullptr || !params.is_object()) return false;
	try {
		/* Shift means estimate in native construction. Ignoring an unexpected
		 * field here could turn an estimate request into an actual command. */
		if (params.contains("shift")) return false;
		if (!params.contains("gesture") || !params.at("gesture").is_string()) return false;
		const std::string &gesture = params.at("gesture").get_ref<const std::string &>();
		const bool move = gesture == "move";
		const bool text = gesture == "text";
		const bool control = gesture == "control";
		if (!control && (params.contains("control") || params.contains("orientation"))) return false;
		bool ctrl = false;
		std::string button;
		if (move || text || control) {
			if (params.contains("button") || params.contains("end_x") || params.contains("end_y") || params.contains("ctrl")) return false;
			if ((move || control) && params.contains("text")) return false;
		} else {
			if (params.contains("text")) return false;
			if (params.contains("ctrl")) {
				if (!params.at("ctrl").is_boolean()) return false;
				ctrl = params.at("ctrl").get<bool>();
			}
			if (!params.contains("button") || !params.at("button").is_string()) return false;
			button = params.at("button").get<std::string>();
			if ((gesture != "click" && gesture != "drag") || (button != "left" && button != "right") || (gesture == "drag" && button != "left")) return false;
		}
		int x, y, end_x, end_y;
		if (!ReadCoordinate(params, "x", _screen.width, x) || !ReadCoordinate(params, "y", _screen.height, y)) return false;
		end_x = x;
		end_y = y;
		if (gesture == "drag" && (!ReadCoordinate(params, "end_x", _screen.width, end_x) || !ReadCoordinate(params, "end_y", _screen.height, end_y))) return false;
		if (_access_origin.policy_dropdown != nullptr && !AccessPolicyDropdown(_access_origin.policy_dropdown)) {
			Window *stale = const_cast<Window *>(_access_origin.policy_dropdown);
			_access_origin.policy_dropdown = nullptr;
			if (IsLiveWindow(stale)) stale->OnFocusLost(false);
			return false;
		}
		/* Cancel invalid story callbacks before InputIsIdle can reject them and
		 * leave every subsequent gesture behind an unusable native tool. */
		if (!ValidateStoryPlacement() || !InputIsIdle()) return false;
		Window *start = FindWindowFromPt(x, y);
		Window *end = FindWindowFromPt(end_x, end_y);
		if (control) {
			/* UI-only semantic selections still use this exact parked image and
			 * its visible hit-tested window. No entity IDs or map targets enter. */
			if (!IsAllowedTarget(start, x, y) || start->IsShaded() || !params.contains("image_id") || !params.at("image_id").is_string() ||
					!params.contains("control") || !params.at("control").is_string()) return false;
			const std::string &kind = params.at("control").get_ref<const std::string &>();
			for (auto it = params.begin(); it != params.end(); ++it) {
				if (it.key() != "image_id" && it.key() != "request_id" && it.key() != "authority_instance_id" && it.key() != "world_epoch" &&
						it.key() != "gesture" && it.key() != "control" && it.key() != "x" && it.key() != "y" &&
						(kind != "road_stop.orientation" || it.key() != "orientation")) return false;
			}
			if (kind == "orders.go_to") return RendererArmOrdersGoTo(start);
			if (kind != "road_stop.orientation" || !params.contains("orientation") || !params.at("orientation").is_string()) return false;
			static constexpr std::array<std::string_view, 6> orientations = {
				"terminal_ne", "terminal_se", "terminal_sw", "terminal_nw", "drive_through_ne_sw", "drive_through_se_nw"};
			const std::string &selected = params.at("orientation").get_ref<const std::string &>();
			const auto found = std::ranges::find(orientations, selected);
			return found != orientations.end() && RendererSetRoadStopOrientation(start, static_cast<uint8_t>(found - orientations.begin()));
		}
		if (text) {
			/* Text neither holds buttons nor dispatches a mouse loop. Invalid
			 * payloads leave both the editbox and native placement state alone. */
			return ApplyQueryText(start, x, y, params);
		}
		/* A sign move must not silently turn a dirty/empty rename into a
		 * deletion or a second command. Save/Cancel/text and camera input remain
		 * available; this fence applies only to an actual left map placement. */
		if (!move && button == "left" && start != nullptr && IsPtInWindowViewport(start, x, y) != nullptr) {
			Window *callback = _thd.GetCallbackWnd();
			if (callback != nullptr && callback->window_class == WindowClass::QueryString &&
					callback->window_number == QueryStringWindowNumber::Sign && !RendererSignEditorCanMove(callback)) {
				ResetObjectToPlace();
				return false;
			}
		}
		const bool read_only = move || button == "right";
		if (!IsAllowedTarget(start, x, y, read_only) || !IsAllowedTarget(end, end_x, end_y, read_only)) return false;
		const bool access_window = start != nullptr && (start->window_class == WindowClass::NetworkClientList || (start->window_class == WindowClass::DropdownMenu && start->parent == _access_origin.window));
		if (access_window) {
			if (gesture == "drag" || text || !params.contains("image_id") || params.at("image_id") != _access_origin.image_id || !AccessSnapshotCurrent()) return false;
			if (!read_only && start->window_class == WindowClass::NetworkClientList) _access_origin.armed = NetworkClientListAccessTargetAt(start, {x - start->left, y - start->top});
			if (!read_only && start->window_class == WindowClass::DropdownMenu) _access_origin.armed_policy = AccessPolicyDropdown(start);
		}
		/* Drag release belongs to the original native callback window. An
		 * overlapping window must never turn its visible pixels into a hidden drop. */
		if (gesture == "drag" && end != start) return false;
		Window *previous_dropdown = FindWindowById(WindowClass::DropdownMenu, 0);
		Window *previous_query = FindWindowById(WindowClass::QueryString, QueryStringWindowNumber::Default);
		const InputOrigin origin{start, start->window_class, start->window_number, GetWidgetFromPos(start, x - start->left, y - start->top)};
		/* A button drag can post on the first native mouse pass too. Reject it
		 * before dispatch rather than close an uncaptured question locally. */
		if (gesture == "drag" && origin.window_class == WindowClass::GoalQuestion &&
				origin.widget >= WID_GQ_BUTTON_1 && origin.widget <= WID_GQ_BUTTON_3) return false;
		/* The normal handler posts before closing this exact popup. Retain only
		 * this gesture's actual source, never choose among duplicate qids later. */
		if (gesture == "click" && button == "left" && origin.window_class == WindowClass::GoalQuestion &&
				origin.widget >= WID_GQ_BUTTON_1 && origin.widget <= WID_GQ_BUTTON_3) _question_origin = origin;
		const Window *previous_confirm = start->FindChildWindowById(WindowClass::ConfirmPopupQuery, ConfirmPopupQueryWindowNumber::Default);
		const Window *previous_date = start->FindChildWindowById(WindowClass::SetDate, start->window_number);
		const WidgetID toolbar_widget = start->window_class == WindowClass::MainToolbar ? GetWidgetFromPos(start, x - start->left, y - start->top) : INVALID_WIDGET;
		std::optional<GameOptionsSettingState> pre_setting;
		if (gesture == "click" && button == "left") {
			if (origin.window_class == WindowClass::GameOptions && origin.widget == WID_GO_OPTIONSPANEL) {
				GameOptionsHit hit = ClassifyGameOptionsHit(start, {x - start->left, y - start->top});
				if (hit.kind == GameOptionsHitKind::Setting && IsAllowedCompanySettingRow(hit.setting)) {
					pre_setting = hit.setting;
					_company_setting_gesture = {origin, {x - start->left, y - start->top}, hit.setting, false};
				}
			} else if (origin.window_class == WindowClass::QueryString &&
					(origin.widget == WID_QS_OK || origin.widget == WID_QS_DEFAULT) && IsAllowedQuery(start) &&
					_query_origin.source.window_class == WindowClass::GameOptions) {
				_company_setting_gesture = {_query_origin.source, {}, _query_origin.setting, true};
			} else if (origin.window_class == WindowClass::ReplaceVehicle && origin.widget == WID_RV_TRAIN_WAGONREMOVE_TOGGLE &&
					origin.window->owner == _local_company && !origin.window->IsShaded() &&
					origin.number == to_underlying(VehicleType::Train)) {
				_company_setting_gesture.source = origin;
			}
		}
		InputRelease release;
		ClearButtons(ctrl);
		_cursor.in_window = true;
		/* Our two-step toolbar menus must stay open after the opening click.
		 * Options are copied into each native menu at creation time. */
		AutoRestoreBackup autoselect{_settings_client.gui.toolbar_dropdown_autoselect, false};
		_cursor.UpdateCursorPosition(x, y);
		if (move) {
			/* Real pointer movement can dismiss or reveal ordinary native tooltips.
			 * It never holds a button or drains a delayed dropdown selection. */
			HandleMouseEvents();
			if (!RememberStoryPlacement(origin, false)) return false;
			release.completed = true;
			return true;
		}
		if (button == "left") {
			_left_button_down = true;
		} else {
			_right_button_down = true;
			_right_button_clicked = true;
		}
		HandleMouseEvents();
		RememberToolbarDropdown(start, toolbar_widget, previous_dropdown);
		RememberAccessDropdown(start, previous_dropdown);
		if (button == "left") {
			_cursor.UpdateCursorPosition(end_x, end_y);
			HandleMouseEvents();
			/* Autoroad and other placement tools consume the preceding pass's
			 * selection size. Settle it while the button is still held. */
			HandleMouseEvents();
		}
		ClearButtons(ctrl);
		HandleMouseEvents();
		RememberToolbarDropdown(start, toolbar_widget, previous_dropdown);
		RememberAccessDropdown(start, previous_dropdown);
		DrainDropdown(FindWindowById(WindowClass::DropdownMenu, 0));
		if (gesture == "click" && button == "left" && origin.window_class == WindowClass::GameOptions &&
				origin.widget == WID_GO_TAB_ADVANCED && (!IsLiveOrigin(origin) || !PrepareCompanyPolicyView(start))) return false;
		RememberQuery(origin, previous_query, false, pre_setting);
		RememberModal(origin, previous_confirm, previous_date);
		if (!RememberStoryPlacement(origin, button == "left")) return false;
		_last_input_origin = IsLiveOrigin(origin) ? origin : InputOrigin{};
		release.completed = true;
		return true;
	} catch (...) {
		return false;
	}
}

} // namespace Integration
