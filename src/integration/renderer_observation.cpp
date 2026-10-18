/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file renderer_observation.cpp Request-local native map and information-panel pixels. */

#include "../stdafx.h"
#include "renderer_observation.h"
#include "../core/backup_type.hpp"
#include "../gfx_func.h"
#include "../transparency.h"
#include "../viewport_func.h"
#include "../window_gui.h"

#include "../safeguards.h"

namespace Integration {
namespace {

void ValidateDimensions(int width, int height)
{
	if (width <= 0 || height <= 0 || width > static_cast<int>(MAX_OBSERVATION_WIDTH) || height > static_cast<int>(MAX_OBSERVATION_HEIGHT) ||
			static_cast<uint64_t>(width) * height > MAX_OBSERVATION_PIXELS) throw std::invalid_argument("Observation dimensions exceed limits");
}

ObservationPixels CopyPixels(const std::vector<Colour> &pixels, int width, int height)
{
	ObservationPixels result{static_cast<uint32_t>(width), static_cast<uint32_t>(height), std::vector<uint8_t>(static_cast<size_t>(width) * height * 3)};
	for (size_t i = 0; i < pixels.size(); ++i) {
		result.rgb[i * 3] = pixels[i].r;
		result.rgb[i * 3 + 1] = pixels[i].g;
		result.rgb[i * 3 + 2] = pixels[i].b;
	}
	return result;
}

DrawPixelInfo Surface(std::vector<Colour> &pixels, int width, int height)
{
	return {.dst_ptr = pixels.data(), .left = 0, .top = 0, .width = width, .height = height, .pitch = width, .zoom = ZoomLevel::Min};
}

void SetLabel(DisplayOption option, bool enabled)
{
	if (enabled) _display_opt.Set(option); else _display_opt.Reset(option);
}

} // namespace

ObservationPixels DrawWorldObservation(const Viewport &viewport, const ObservationLabels &labels)
{
	ValidateDimensions(viewport.width, viewport.height);
	std::vector<Colour> pixels(static_cast<size_t>(viewport.width) * viewport.height);
	DrawPixelInfo dpi = Surface(pixels, viewport.width, viewport.height);
	/* The 32bpp blitter reads _screen.pitch even when a separate DPI is supplied. */
	AutoRestoreBackup screen_backup(_screen, dpi);
	AutoRestoreBackup dpi_backup(_cur_dpi, &dpi);
	AutoRestoreBackup animation_backup(_screen_disable_anim, true);
	AutoRestoreBackup display_backup(_display_opt);
	AutoRestoreBackup transparency_backup(_transparency_opt, TransparencyOptions{});
	AutoRestoreBackup invisibility_backup(_invisibility_opt, TransparencyOptions{});
	AutoRestoreBackup facility_backup(_facility_display_opt);
	_facility_display_opt.Set();
	SetLabel(DisplayOption::ShowTownNames, labels.towns);
	SetLabel(DisplayOption::ShowStationNames, labels.stations);
	SetLabel(DisplayOption::ShowWaypointNames, labels.waypoints);
	SetLabel(DisplayOption::ShowSigns, labels.signs);
	SetLabel(DisplayOption::ShowCompetitorSigns, labels.competitors);
	/* Population-label formatting and native font scale remain the worker's fixed profile;
	 * changing population_in_label here would invalidate cached town-sign geometry. */
	ViewportDoDraw(viewport, viewport.virtual_left, viewport.virtual_top,
		viewport.virtual_left + viewport.virtual_width, viewport.virtual_top + viewport.virtual_height);
	return CopyPixels(pixels, viewport.width, viewport.height);
}

ObservationPixels DrawNativePanelObservation(Window &window)
{
	if (window.viewport != nullptr || (window.window_class != WindowClass::Finances && window.window_class != WindowClass::TownDirectory)) {
		throw std::invalid_argument("Unsupported native panel");
	}
	const int width = window.width;
	const int height = window.height;
	ValidateDimensions(width, height);
	std::vector<Colour> pixels(static_cast<size_t>(width) * height);
	DrawPixelInfo dpi = Surface(pixels, width, height);
	AutoRestoreBackup screen_backup(_screen, dpi);
	AutoRestoreBackup dpi_backup(_cur_dpi, &dpi);
	AutoRestoreBackup animation_backup(_screen_disable_anim, true);
	/* OnPaint and widgets use window-relative coordinates, exactly as in the ordinary
	 * compositor. Paint only this prepared real window, so no desktop overlay can occlude it. */
	window.OnPaint();
	if (window.width != width || window.height != height) throw std::runtime_error("Native panel layout changed during drawing");
	return CopyPixels(pixels, width, height);
}

} // namespace Integration
