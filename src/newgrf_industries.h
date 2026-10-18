/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 * OpenTTD is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
 * See the GNU General Public License for more details. You should have received a copy of the GNU General Public License along with OpenTTD. If not, see <https://www.gnu.org/licenses/old-licenses/gpl-2.0>.
 */

/** @file newgrf_industries.h Functions for NewGRF industries. */

#ifndef NEWGRF_INDUSTRIES_H
#define NEWGRF_INDUSTRIES_H

#include "newgrf_town.h"

#include <array>
#include <map>
#include <optional>

struct Industry;
struct Town;

/** Request-local callback writes, allocated only for encountered owners; no pool items. */
struct IndustryPresentationPsaOverlay final {
	static constexpr size_t CELL_COUNT = 256;
	using Writes = std::array<std::optional<int32_t>, CELL_COUNT>;
	const Industry *industry; ///< Live industry whose callback sequence this overlay belongs to.
	const Town *town; ///< The industry's live parent town, if present.
	GrfID active_grfid; ///< GRFID from the resolving industry spec, not caller input.
	std::map<const Industry *, Writes> industry_writes;
	std::map<std::pair<const Town *, GrfID>, Writes> town_writes;

	IndustryPresentationPsaOverlay(const Industry *industry, const Town *town, GrfID active_grfid) :
		industry(industry), town(town), active_grfid(active_grfid) {}
};

/** Resolver for industry scopes. */
struct IndustriesScopeResolver : public ScopeResolver {
	TileIndex tile;     ///< Tile owned by the industry.
	Industry *industry; ///< %Industry being resolved.
	IndustryType type;  ///< Type of the industry.
	uint32_t random_bits; ///< Random bits of the new industry.
	IndustryPresentationPsaOverlay *presentation_psa = nullptr; ///< Query-local shadow writes, if any.

	/**
	 * Scope resolver for industries.
	 * @param ro Surrounding resolver.
	 * @param tile %Tile owned by the industry.
	 * @param industry %Industry being resolved.
	 * @param type Type of the industry.
	 * @param random_bits Random bits of the new industry.
	 */
	IndustriesScopeResolver(ResolverObject &ro, TileIndex tile, Industry *industry, IndustryType type, uint32_t random_bits = 0,
			IndustryPresentationPsaOverlay *presentation_psa = nullptr)
		: ScopeResolver(ro), tile(tile), industry(industry), type(type), random_bits(random_bits), presentation_psa(presentation_psa)
	{
	}

	uint32_t GetRandomBits() const override;
	uint32_t GetVariable(uint8_t variable, [[maybe_unused]] uint32_t parameter, bool &available) const override;
	uint32_t GetRandomTriggers() const override;
	void StorePSA(uint pos, int32_t value) override;
};

/** Resolver for industries. */
struct IndustriesResolverObject : public ResolverObject {
	IndustriesScopeResolver industries_scope; ///< Scope resolver for the industry.
	std::optional<TownScopeResolver> town_scope = std::nullopt; ///< Scope resolver for the associated town (if needed and available, else \c std::nullopt).
	IndustryPresentationPsaOverlay *presentation_psa = nullptr; ///< Explicit request-local overlay, or normal live PSA path.

	IndustriesResolverObject(TileIndex tile, Industry *indus, IndustryType type, uint32_t random_bits = 0,
			CallbackID callback = CBID_NO_CALLBACK, uint32_t callback_param1 = 0, uint32_t callback_param2 = 0);
	IndustriesResolverObject(TileIndex tile, Industry *indus, IndustryType type, uint32_t random_bits,
			CallbackID callback, uint32_t callback_param1, uint32_t callback_param2,
			IndustryPresentationPsaOverlay *presentation_psa);

	TownScopeResolver *GetTown();

	ScopeResolver *GetScope(VarSpriteGroupScope scope = VarSpriteGroupScope::Self, uint8_t relative = 0) override
	{
		switch (scope) {
			case VarSpriteGroupScope::Self: return &industries_scope;
			case VarSpriteGroupScope::Parent: {
				TownScopeResolver *tsr = this->GetTown();
				if (tsr != nullptr) return tsr;
			}
			[[fallthrough]];

			default:
				return ResolverObject::GetScope(scope, relative);
		}
	}

	GrfSpecFeature GetFeature() const override;
	uint32_t GetDebugID() const override;
};

/** From where has callback #CBID_INDUSTRY_PROBABILITY been called */
enum class IndustryAvailabilityCallType : uint8_t {
	MapGeneration, ///< during random map generation
	RandomCreation, ///< during creation of random ingame industry
	UserCreation, ///< from the Fund/build window
	ProspectCreation, ///< from the Fund/build using prospecting
};

/* in newgrf_industry.cpp */
uint16_t GetIndustryCallback(CallbackID callback, uint32_t param1, uint32_t param2, Industry *industry, IndustryType type, TileIndex tile, std::span<int32_t> regs100 = {});
uint16_t GetIndustryCallbackForObservation(CallbackID callback, uint32_t param1, uint32_t param2, Industry *industry,
		IndustryType type, TileIndex tile, std::span<int32_t> regs100, IndustryPresentationPsaOverlay &presentation_psa);
uint32_t GetIndustryIDAtOffset(TileIndex new_tile, const Industry *i, GrfID cur_grfid);
void IndustryProductionCallback(Industry *ind, int reason);
CommandCost CheckIfCallBackAllowsCreation(TileIndex tile, IndustryType type, size_t layout, uint32_t seed, uint16_t initial_random_bits, Owner founder, IndustryAvailabilityCallType creation_type);
uint32_t GetIndustryProbabilityCallback(IndustryType type, IndustryAvailabilityCallType creation_type, uint32_t default_prob);
bool IndustryTemporarilyRefusesCargo(Industry *ind, CargoType cargo_type);

IndustryType MapNewGRFIndustryType(IndustryType grf_type, GrfID grfid);

/* in newgrf_industrytiles.cpp*/
uint32_t GetNearbyIndustryTileInformation(uint8_t parameter, TileIndex tile, IndustryID index, bool signed_offsets, bool grf_version8);

#endif /* NEWGRF_INDUSTRIES_H */
