/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 * OpenTTD is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
 * See the GNU General Public License for more details. You should have received a copy of the GNU General Public License along with OpenTTD. If not, see <https://www.gnu.org/licenses/old-licenses/gpl-2.0>.
 */

/** @file renderer_input.h Private, native UI gestures for a parked rendering replica. */

#ifndef INTEGRATION_RENDERER_INPUT_H
#define INTEGRATION_RENDERER_INPUT_H

#include "../3rdparty/nlohmann/json.hpp"
#include "../command_type.h"
#include "../story_type.h"
#include "../company_type.h"
#include "../network/network_type.h"
#include "../goal_type.h"
#include "../window_type.h"
#include "../core/geometry_type.hpp"
#include <optional>

struct Window;
struct QueryString;


namespace Integration {

/** Finite native client-list roles; all other matrix buttons remain denied. */
enum class ClientListAccessKind : uint8_t { Denied, Authorize, Revoke, OwnPolicyMenu };
struct ClientListAccessTarget {
	ClientListAccessKind kind = ClientListAccessKind::Denied;
	CompanyID company = CompanyID::Invalid();
	ClientID client = ClientID::Invalid;
	uint64_t local_generation = 0;
	std::string key;
};
/** Inspect only the concrete native client-list window/model. Private to the renderer. */
std::string NetworkClientListAccessVersion(const Window *window);
std::optional<ClientListAccessTarget> NetworkClientListAccessTargetAt(const Window *window, Point relative);
void RendererRememberAccessSnapshot(std::string_view image_id);
void RendererClearAccessSnapshot();
bool RendererAccessCommandMatches(CompanyAllowListCtrlAction action, std::string_view key);
/** Permit only the company-setting Post witnessed by this exact native GUI gesture. */
bool RendererCompanySettingCommandMatches(std::string_view native_name);

/**
 * Dispatch one bounded native mouse gesture on the game thread.
 * The caller must validate the screenshot and park the UI, with command capture
 * and direct network-send guards active throughout this call and its cleanup.
 * Click/drag parameters: button (left or right), x, y and optional boolean ctrl;
 * drags also require end_x, end_y and the left button. A move accepts only x/y.
 * Text accepts x/y/text, replaces an approved native query editbox, and does not
 * submit it. Coordinates must be JSON integers. Move/text forbid ctrl/button.
 * A true result means approved controls were dispatched, possibly without a
 * command. On false, discard any captured commands; local UI changes can remain.
 * Native repeated-click/double-click behaviour is preserved. No key events.
 */
bool RendererApplyInput(const nlohmann::json &params);

/** Arm ordinary Go To in this visible native Orders window without toggling it off. */
bool RendererArmOrdersGoTo(Window *window);
/** Select an explicit ordinary road-stop picker orientation (NE/SE/SW/NW, then NE-SW/SE-NW drive-through). */
bool RendererSetRoadStopOrientation(Window *window, uint8_t orientation);

/** Return only the concrete normal town-funding name buffer; no generic inline editor. */
QueryString *RendererFoundTownNameQuery(Window *window);
/** Inspect only the concrete native sign editor and its live editable sign. */
bool RendererSignEditorEditable(const Window *window);
/** A live named sign with no pending editor change can move in one captured command. */
bool RendererSignEditorCanMove(const Window *window);

/** Concrete native StoryBook inspection; caller supplies a live window. Invalid means no active selection. */
StoryPageElementID RendererStoryPlacementElement(const Window *window);

/** Inspect this exact live concrete popup and clicked offered widget, never a qid lookup. */
std::optional<nlohmann::json> RendererGoalQuestionWindowWitness(const Window *window, WidgetID widget, uint16_t id, GoalQuestionButton button);
/** Available only inside the one approved question-button gesture's captured Post. */
std::optional<nlohmann::json> RendererQuestionCommandWitness(uint16_t id, GoalQuestionButton button);

/** Forget exact dialog/parent instance registrations before a native window closes. */
void RendererInputWindowClosed(const Window *window);
/** Invalidate registrations across map/disconnect lifecycle revisions. */
void RendererResetInputProvenance();
/** Register only known UI-only dialogs opened by this gesture's native owner callback. */
void RendererBeginInputCallback(Commands command);
void RendererEndInputCallback();

} // namespace Integration

#endif /* INTEGRATION_RENDERER_INPUT_H */
