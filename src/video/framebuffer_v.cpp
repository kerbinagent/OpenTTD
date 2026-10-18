/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 * OpenTTD is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
 * See the GNU General Public License for more details. You should have received a copy of the GNU General Public License along with OpenTTD. If not, see <https://www.gnu.org/licenses/old-licenses/gpl-2.0>.
 */

/** @file framebuffer_v.cpp A software-rendered game client without an OS window. */

#include "../stdafx.h"
#include "../blitter/factory.hpp"
#include "../gfx_func.h"
#include "../window_func.h"
#include "framebuffer_v.h"
#include "../integration/renderer.h"

#include "../safeguards.h"

static FVideoDriver_Framebuffer _framebuffer_driver;

static constexpr int FRAMEBUFFER_MAX_WIDTH = 2560;
static constexpr int FRAMEBUFFER_MAX_HEIGHT = 1440;

void VideoDriver_Framebuffer::Allocate(int width, int height)
{
	this->pixels.assign(static_cast<size_t>(width) * height, Colour{});
	_cur_resolution = {static_cast<uint>(width), static_cast<uint>(height)};
	_screen.width = _screen.pitch = width;
	_screen.height = height;
	_screen.dst_ptr = this->pixels.data();
	ScreenSizeChanged();
	BlitterFactory::GetCurrentBlitter()->PostResize();
	MarkWholeScreenDirty();
}

std::optional<std::string_view> VideoDriver_Framebuffer::Start(const StringList &params)
{
	/* A null/animation/hardware blitter cannot supply this ordinary RGB framebuffer. */
	if (BlitterFactory::SelectBlitter("32bpp-optimized") == nullptr) return "The framebuffer driver requires the 32bpp-optimized blitter";
	int width = GetDriverParamInt(params, "width", _cur_resolution.width == 0 ? 1280 : _cur_resolution.width);
	int height = GetDriverParamInt(params, "height", _cur_resolution.height == 0 ? 720 : _cur_resolution.height);
	if (width < 640 || width > FRAMEBUFFER_MAX_WIDTH || height < 480 || height > FRAMEBUFFER_MAX_HEIGHT) return "Framebuffer size must be between 640x480 and 2560x1440";
	/* All private IPC, input and rendering share this client's single game thread. */
	this->is_game_threaded = false;
	this->Allocate(width, height);
	return std::nullopt;
}

void VideoDriver_Framebuffer::Stop()
{
	_screen.dst_ptr = nullptr;
	this->pixels.clear();
}

void VideoDriver_Framebuffer::MainLoop()
{
	while (!_exit_game) {
		if (Integration::RendererIsActive()) {
			const auto next = std::chrono::steady_clock::now() + this->GetGameInterval();
			/* IPC and network reception continue; only this replica's replay/UI are parked. */
			::GameLoop();
			if (!Integration::RendererShouldPark()) {
				this->DrainCommandQueue();
				::InputLoop();
				::UpdateWindows();
			}
			std::this_thread::sleep_until(next);
			continue;
		}
		this->Tick();
		this->SleepTillNextTick();
	}
}

bool VideoDriver_Framebuffer::ChangeResolution(int width, int height)
{
	if (width < 640 || width > FRAMEBUFFER_MAX_WIDTH || height < 480 || height > FRAMEBUFFER_MAX_HEIGHT) return false;
	this->Allocate(width, height);
	GameSizeChanged();
	return true;
}

bool VideoDriver_Framebuffer::AfterBlitterChange()
{
	return BlitterFactory::GetCurrentBlitter()->GetName() == "32bpp-optimized";
}
