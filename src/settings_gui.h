/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 * OpenTTD is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
 * See the GNU General Public License for more details. You should have received a copy of the GNU General Public License along with OpenTTD. If not, see <https://www.gnu.org/licenses/old-licenses/gpl-2.0>.
 */

/** @file settings_gui.h Functions for setting GUIs. */

#ifndef SETTING_GUI_H
#define SETTING_GUI_H

#include "gfx_type.h"
#include "dropdown_type.h"

#include <cstdint>
#include <optional>

struct Window;
struct SettingEntry;
struct IntSettingDesc;

/** Finite hit classification for the native Game Options company-policy view. */
enum class GameOptionsHitKind : uint8_t {
	Denied,
	Navigation,
	Setting,
};

/** Exact native row identity; callers must check the company-policy registry. */
struct GameOptionsSettingState {
	const SettingEntry *entry = nullptr;
	const IntSettingDesc *descriptor = nullptr;
};

struct GameOptionsHit {
	GameOptionsHitKind kind = GameOptionsHitKind::Denied;
	GameOptionsSettingState setting{};
};

GameOptionsHit ClassifyGameOptionsHit(const Window *window, Point local);
std::optional<GameOptionsSettingState> CurrentGameOptionsValueEntry(const Window *window);
bool PrepareCompanyPolicyView(Window *window);

/** Width of setting buttons */
#define SETTING_BUTTON_WIDTH  ((int)NWidgetScrollbar::GetHorizontalDimension().width * 2)
/** Height of setting buttons */
#define SETTING_BUTTON_HEIGHT ((int)NWidgetScrollbar::GetHorizontalDimension().height)

void DrawArrowButtons(int x, int y, Colours button_colour, uint8_t state, bool clickable_left, bool clickable_right);
void DrawUpDownButtons(int x, int y, Colours button_colour, uint8_t state, bool clickable_up, bool clickable_down);
void DrawDropDownButton(int x, int y, Colours button_colour, bool state, bool clickable);
void DrawBoolButton(int x, int y, Colours button_colour, Colours background, bool state, bool clickable);

template <class T>
DropDownList BuildSetDropDownList(int *selected_index);

/* Actually implemented in music_gui.cpp */
void ChangeMusicSet(int index);

#endif /* SETTING_GUI_H */

