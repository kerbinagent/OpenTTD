/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 * OpenTTD is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
 * See the GNU General Public License for more details. You should have received a copy of the GNU General Public License along with OpenTTD. If not, see <https://www.gnu.org/licenses/old-licenses/gpl-2.0>.
 */

/** @file framebuffer_v.h Display-independent, genuine GUI rendering. */

#ifndef VIDEO_FRAMEBUFFER_H
#define VIDEO_FRAMEBUFFER_H

#include "video_driver.hpp"

/** A real software framebuffer with no desktop or display-server dependency. */
class VideoDriver_Framebuffer : public VideoDriver {
public:
	std::optional<std::string_view> Start(const StringList &params) override;
	void Stop() override;
	void MakeDirty(int, int, int, int) override {}
	void MainLoop() override;
	bool ChangeResolution(int width, int height) override;
	bool ToggleFullscreen(bool) override { return false; }
	bool AfterBlitterChange() override;
	std::string_view GetName() const override { return "framebuffer"; }

private:
	std::vector<Colour> pixels;
	void Allocate(int width, int height);
};

class FVideoDriver_Framebuffer : public DriverFactoryBase {
public:
	/* Explicit selection only; desktop clients keep their usual driver. */
	FVideoDriver_Framebuffer() : DriverFactoryBase(Driver::Type::Video, 0, "framebuffer", "Display-independent GUI framebuffer") {}
	std::unique_ptr<Driver> CreateInstance() const override { return std::make_unique<VideoDriver_Framebuffer>(); }
};

#endif /* VIDEO_FRAMEBUFFER_H */
