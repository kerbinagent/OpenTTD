/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file industry_presentation.h Shared IndustryView presentation sources. */

#ifndef INDUSTRY_PRESENTATION_H
#define INDUSTRY_PRESENTATION_H

#include <cstdint>
#include <string>

struct Industry;

/** Finite text sources exposed by the industry view. */
enum class IndustryDisplayTextKind : uint8_t {
	AcceptedLine,
	ProducedLine,
	ExtraText,
	Name,
	CustomText,
};

/** Render one source after the caller has validated the live industry and selected slot. */
std::string RenderIndustryDisplayText(const Industry &industry, IndustryDisplayTextKind kind, uint8_t slot = 0);

#endif /* INDUSTRY_PRESENTATION_H */
