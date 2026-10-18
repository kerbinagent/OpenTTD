/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file test_integration_construction.cpp Construction permission and queued-intent dependencies. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/actions_internal.h"
#include "../integration/renderer_input.h"
#include "../clear_map.h"
#include "../company_base.h"
#include "../company_func.h"
#include "../core/pool_func.hpp"
#include "../core/endian_func.hpp"
#include "../depot_base.h"
#include "../gfx_func.h"
#include "../language.h"
#include "../misc/endian_buffer.hpp"
#include "../newgrf_object.h"
#include "../newgrf_station.h"
#include "../object_base.h"
#include "../object_cmd.h"
#include "../object_map.h"
#include "../network/network.h"
#include "../openttd.h"
#include "../querystring_gui.h"
#include "../road_cmd.h"
#include "../road.h"
#include "../road_map.h"
#include "../signs_base.h"
#include "../signs_cmd.h"
#include "../signs_func.h"
#include "../strings_func.h"
#include "../station_base.h"
#include "../tilehighlight_func.h"
#include "../video/video_driver.hpp"
#include "../viewport_kdtree.h"
#include "../waypoint_base.h"
#include "../window_gui.h"
#include "../window_func.h"
#include "../widgets/sign_widget.h"
#include "mock_environment.h"

#include <fstream>

#include "table/strings.h"
#include "../table/sprites.h"

#include "../safeguards.h"

namespace {

using Integration::Json;
using namespace Integration::Detail;

struct ConstructionWorld {
	CompanyID previous_company = _current_company;
	Company *company;
	TileIndex tile;

	ConstructionWorld()
	{
		_sign_pool.CleanPool();
		_depot_pool.CleanPool();
		_station_pool.CleanPool();
		_object_pool.CleanPool();
		_company_pool.CleanPool();
		Map::Allocate(64, 64);
		StationClass::Reset();
		ResetObjects();
		ResetRoadTypes();
		REQUIRE(Company::CanAllocateItem());
		company = Company::Create();
		_current_company = company->index;
		tile = TileXY(20, 20);
		for (uint y = 0; y < Map::SizeY(); ++y) {
			for (uint x = 0; x < Map::SizeX(); ++x) {
				TileIndex tile = TileXY(x, y);
				if (IsInnerTile(tile)) MakeClear(tile, ClearGround::Grass, 3);
			}
		}
	}

	~ConstructionWorld()
	{
		_sign_pool.CleanPool();
		_depot_pool.CleanPool();
		_station_pool.CleanPool();
		_object_pool.CleanPool();
		_company_pool.CleanPool();
		_current_company = previous_company;
	}

	Sign *PlaceSign(Owner owner, const std::string &name = "shared marker") const
	{
		REQUIRE(Sign::CanAllocateItem());
		return Sign::Create(owner, TileX(tile) * TILE_SIZE, TileY(tile) * TILE_SIZE, 0, name);
	}
};

template <Commands Tcommand, typename ... T>
CommandDataBuffer NativeData(T &&... values)
{
	using Args = typename CommandTraits<Tcommand>::Args;
	return EndianBufferWriter<CommandDataBuffer>::FromValue(Args{std::forward<T>(values)...});
}

} // namespace

namespace {

/** Native edit boxes need a video focus sink as well as layout fonts/sprites. */
struct SignEditorWorld {
	ConstructionWorld world;
	DrawPixelInfo previous_screen = _screen;
	CursorVars previous_cursor = _cursor;
	GameMode previous_mode = _game_mode;
	CompanyID previous_local = _local_company;
	bool previous_networking = _networking;
	PauseModes previous_pause = _pause_mode;
	std::optional<std::string> previous_role;
	std::vector<uint8_t> pixels = std::vector<uint8_t>(1024 * 768);
	Window *editor = nullptr;

	static void Role(const char *value)
	{
#ifdef _WIN32
		REQUIRE(_putenv_s("OPENTTD_INTEGRATION_ROLE", value == nullptr ? "" : value) == 0);
#else
		REQUIRE((value == nullptr ? unsetenv("OPENTTD_INTEGRATION_ROLE") : setenv("OPENTTD_INTEGRATION_ROLE", value, 1)) == 0);
#endif
	}

	SignEditorWorld()
	{
		if (const char *role = std::getenv("OPENTTD_INTEGRATION_ROLE"); role != nullptr) this->previous_role = role;
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
		/* Install the ordinary null driver once if the unit process has no
		 * driver yet; no desktop, renderer socket or event loop is started. */
		if (VideoDriver::GetInstance() == nullptr) DriverFactoryBase::SelectDriver("null", Driver::Type::Video);
		_screen.width = 1024;
		_screen.height = 768;
		_screen.pitch = 1024;
		_screen.dst_ptr = this->pixels.data();
		ScreenSizeChanged();
		SetupWidgetDimensions();
		SetMouseCursor(SPR_CURSOR_MOUSE, PAL_NONE);
		_game_mode = GameMode::Normal;
		_local_company = _current_company;
		_networking = false;
		_pause_mode = {};
		Role("renderer");
	}

	~SignEditorWorld()
	{
		ResetObjectToPlace();
		if (this->editor != nullptr) {
			for (Window *window : Window::Iterate()) if (window == this->editor) { window->Close(); break; }
		}
		Window::DeleteClosedWindows();
		/* The ordinary rename/move handlers update the native sign index. The
		 * pool fixture bypasses command deletion, so release those entries too. */
		for (Sign *sign : Sign::Iterate()) {
			if (sign->sign.kdtree_valid) {
				_viewport_sign_kdtree.Remove(ViewportSignKdtreeItem::MakeSign(sign->index));
				sign->sign.kdtree_valid = false;
			}
		}
		Integration::RendererResetInputProvenance();
#ifdef _WIN32
		_putenv_s("OPENTTD_INTEGRATION_ROLE", this->previous_role.has_value() ? this->previous_role->c_str() : "");
#else
		if (this->previous_role.has_value()) setenv("OPENTTD_INTEGRATION_ROLE", this->previous_role->c_str(), 1);
		else unsetenv("OPENTTD_INTEGRATION_ROLE");
#endif
		_game_mode = this->previous_mode;
		_local_company = this->previous_local;
		_networking = this->previous_networking;
		_pause_mode = this->previous_pause;
		_cursor = std::move(this->previous_cursor);
		_screen = this->previous_screen;
		ScreenSizeChanged();
	}

	Window *Open(const Sign *sign)
	{
		ShowRenameSignWindow(sign);
		this->editor = FindWindowById(WindowClass::QueryString, QueryStringWindowNumber::Sign);
		REQUIRE(this->editor != nullptr);
		return this->editor;
	}

	bool Text(const std::string &text) const
	{
		const Rect rect = this->editor->GetWidget<NWidgetBase>(WID_QES_TEXT)->GetCurrentRect();
		return Integration::RendererApplyInput({{"gesture", "text"}, {"x", this->editor->left + (rect.left + rect.right) / 2},
			{"y", this->editor->top + (rect.top + rect.bottom) / 2}, {"text", text}});
	}

	bool Live() const
	{
		for (const Window *window : Window::Iterate()) if (window == this->editor) return true;
		return false;
	}
};

} // namespace

TEST_CASE("Native sign editor retains shared permissions and fences its exact incarnation", "[integration][construction][sign-editor]")
{
	SignEditorWorld fixture;
	for (Owner owner : {Owner(_current_company), OWNER_NONE, Owner(CompanyID(1))}) {
		Sign *sign = fixture.world.PlaceSign(owner);
		Window *editor = fixture.Open(sign);
		CHECK(Integration::RendererSignEditorEditable(editor));
		CHECK(Integration::RendererSignEditorCanMove(editor));
	}
	Sign *protected_sign = fixture.world.PlaceSign(OWNER_DEITY);
	CHECK_FALSE(Integration::RendererSignEditorEditable(fixture.Open(protected_sign)));
	CHECK_FALSE(Integration::RendererSignEditorCanMove(fixture.editor));
	fixture.editor->Close();
	_sign_pool.CleanPool();
	Sign *sign = fixture.world.PlaceSign(_current_company);
	Window *editor = fixture.Open(sign);
	const auto id = sign->index;
	const auto generation = sign->GetInstanceGeneration();
	/* Pool replacement normally closes UI during world reset. Retaining this
	 * real editor deliberately exercises the local ABA defense itself. */
	_sign_pool.CleanPool();
	Sign *replacement = fixture.world.PlaceSign(_current_company);
	REQUIRE(replacement->index == id);
	CHECK(replacement->GetInstanceGeneration() > generation);
	CHECK_FALSE(Integration::RendererSignEditorEditable(editor));
	CHECK_FALSE(Integration::RendererSignEditorCanMove(editor));
	editor->Close();
	CHECK_FALSE(Integration::RendererSignEditorEditable(editor));
	CHECK_FALSE(Integration::RendererSignEditorEditable(nullptr));
}

TEST_CASE("Native sign text uses exact roundtrip and dirty or empty movement never closes it", "[integration][construction][sign-editor]")
{
	SignEditorWorld fixture;
	Sign *sign = fixture.world.PlaceSign(_current_company);
	fixture.Open(sign);
	REQUIRE(fixture.Text("renamed separately"));
	CHECK(sign->name == "shared marker");
	CHECK(Integration::RendererSignEditorEditable(fixture.editor));
	CHECK_FALSE(Integration::RendererSignEditorCanMove(fixture.editor));
	CHECK_FALSE(fixture.Text(std::string(MAX_LENGTH_SIGN_NAME_CHARS, 'x')));
	CHECK(std::string(fixture.editor->GetQueryString(WID_QES_TEXT)->text.GetText()) == "renamed separately");
	fixture.editor->OnClick({}, WID_QES_MOVE, 1);
	fixture.editor->OnPlaceObject({}, TileXY(22, 22));
	CHECK(fixture.Live());
	CHECK(sign->name == "shared marker");
	CHECK(sign->x == 20 * TILE_SIZE);
	CHECK(_thd.GetCallbackWnd() == nullptr);
	REQUIRE(fixture.Text(""));
	fixture.editor->OnClick({}, WID_QES_MOVE, 1);
	fixture.editor->OnPlaceObject({}, TileXY(23, 23));
	CHECK(fixture.Live());
	CHECK(Sign::GetIfValid(sign->index) == sign);
	CHECK(_thd.GetCallbackWnd() == nullptr);
}

TEST_CASE("Unnamed sign movement and pending sign colour are rejected before either native Post", "[integration][construction][sign-editor]")
{
	SignEditorWorld fixture;
	Sign *sign = fixture.world.PlaceSign(_current_company, "");
	fixture.Open(sign);
	CHECK(Integration::RendererSignEditorEditable(fixture.editor));
	CHECK_FALSE(Integration::RendererSignEditorCanMove(fixture.editor));
	fixture.editor->OnClick({}, WID_QES_MOVE, 1);
	fixture.editor->OnPlaceObject({}, TileXY(24, 24));
	CHECK(fixture.Live());
	CHECK(Sign::GetIfValid(sign->index) == sign);
	sign->name = "saved name";
	fixture.Open(sign);
	CHECK(Integration::RendererSignEditorCanMove(fixture.editor));
	fixture.editor->OnDropdownSelect(WID_QES_COLOUR, to_underlying(Colours::Red), 0);
	CHECK_FALSE(Integration::RendererSignEditorCanMove(fixture.editor));
	fixture.editor->OnClick({}, WID_QES_MOVE, 1);
	fixture.editor->OnPlaceObject({}, TileXY(24, 24));
	CHECK(fixture.Live());
	CHECK(sign->text_colour == Colours::White);
}

TEST_CASE("Renderer sign movement requires saving its name first and then closes the exact editor", "[integration][construction][sign-editor]")
{
	SignEditorWorld fixture;
	Sign *sign = fixture.world.PlaceSign(_current_company, "");
	fixture.Open(sign);
	REQUIRE(fixture.Text("saved first"));
	CHECK_FALSE(Integration::RendererSignEditorCanMove(fixture.editor));
	fixture.editor->OnClick({}, WID_QES_OK, 1);
	CHECK_FALSE(fixture.Live());
	/* Simulate the ordinary authoritative RenameSign completion; the isolated
	 * worker never executes captured commands against its replica directly. */
	REQUIRE(CmdRenameSign(DoCommandFlag::Execute, sign->index, "saved first", Colours::Invalid).Succeeded());
	fixture.Open(sign);
	REQUIRE(Integration::RendererSignEditorCanMove(fixture.editor));
	fixture.editor->OnClick({}, WID_QES_MOVE, 1);
	fixture.editor->OnPlaceObject({}, TileXY(25, 25));
	CHECK_FALSE(fixture.Live());
	CHECK(sign->name == "saved first");
	CHECK(sign->x == 20 * TILE_SIZE); // No direct gameplay send outside a parked capture.
}

TEST_CASE("Ordinary human sign input retains its combined rename and move behavior", "[integration][construction][sign-editor]")
{
	SignEditorWorld fixture;
	Sign *sign = fixture.world.PlaceSign(_current_company);
	fixture.Open(sign);
	fixture.editor->GetQueryString(WID_QES_TEXT)->text.Assign("human combined change");
	SignEditorWorld::Role(nullptr);
	fixture.editor->OnClick({}, WID_QES_MOVE, 1);
	fixture.editor->OnPlaceObject({}, TileXY(26, 27));
	CHECK_FALSE(fixture.Live());
	CHECK(sign->name == "human combined change");
	CHECK(sign->x == 26 * TILE_SIZE);
	CHECK(sign->y == 27 * TILE_SIZE);
}

TEST_CASE("Construction signs retain native shared edit permissions and reject stale handles", "[integration][construction]")
{
	ConstructionWorld world;
	Sign *other_company = world.PlaceSign(CompanyID(1));
	Json parameters = {{"sign_id", Handle(other_company)}, {"name", "new marker"}};
	CHECK(CaptureConstructionTargets("sign.rename", parameters).has_value());

	Json wrong_boot = parameters;
	wrong_boot["sign_id"]["engine_instance_id"] = "unrelated-engine-boot";
	CHECK_THROWS_AS(CaptureConstructionTargets("sign.rename", wrong_boot), InvalidParameters);

	const auto old_id = other_company->index;
	const auto old_generation = other_company->GetInstanceGeneration();
	delete other_company;
	Sign *replacement = world.PlaceSign(CompanyID(1));
	REQUIRE(replacement->index == old_id);
	REQUIRE(replacement->GetInstanceGeneration() != old_generation);
	CHECK_THROWS_AS(CaptureConstructionTargets("sign.rename", parameters), InvalidParameters);

	Sign *script_sign = world.PlaceSign(OWNER_DEITY, "protected script text");
	CHECK_THROWS_AS(CaptureConstructionTargets("sign.delete", {{"sign_id", Handle(script_sign)}}), InvalidParameters);
}

TEST_CASE("Neutral waypoint labels are permitted without relaxing other-company ownership", "[integration][construction]")
{
	ConstructionWorld world;
	REQUIRE(Waypoint::CanAllocateItem());
	auto *waypoint = Waypoint::Create(world.tile);
	waypoint->owner = OWNER_NONE;
	waypoint->string_id = STR_SV_STNAME_BUOY;
	Json parameters = {{"station_id", Handle(waypoint)}, {"name", "neutral buoy"}};
	CHECK(CaptureConstructionTargets("waypoint.rename", parameters).has_value());
	waypoint->owner = CompanyID(1);
	CHECK_THROWS_AS(CaptureConstructionTargets("waypoint.rename", parameters), InvalidParameters);
}

TEST_CASE("Construction native proof detects sign changes without comparing process-local generations", "[integration][construction]")
{
	ConstructionWorld world;
	Sign *sign = world.PlaceSign(_current_company);
	const auto data = NativeData<Commands::RenameSign>(sign->index, std::string("renamed"), Colours::Invalid);
	const auto original = ConstructionIntentState(Commands::RenameSign, data);
	REQUIRE(original.has_value());
	sign->name = "changed since screenshot";
	CHECK(ConstructionIntentState(Commands::RenameSign, data) != original);
	sign->name = "shared marker";
	const auto id = sign->index;
	const auto generation = sign->GetInstanceGeneration();
	delete sign;
	sign = world.PlaceSign(_current_company);
	REQUIRE(sign->index == id);
	REQUIRE(sign->GetInstanceGeneration() != generation);
	/* Renderer and authority pools have their own generation sequences. Root's
	 * W(F) check fences incarnations; the comparable proof contains content. */
	CHECK(ConstructionIntentState(Commands::RenameSign, data) == original);
	CommandDataBuffer truncated = data;
	truncated.pop_back();
	CHECK_THROWS_AS(ConstructionIntentState(Commands::RenameSign, truncated), InvalidParameters);
}

TEST_CASE("Construction proof captures old headquarters and one-way road toggle dependencies", "[integration][construction]")
{
	ConstructionWorld world;
	REQUIRE(Object::CanAllocateItem());
	auto *hq = Object::Create(OBJECT_HQ, nullptr, TileArea(world.tile, 2, 2), TimerGameCalendar::date, uint8_t{0});
	MakeObject(world.tile, _current_company, hq->index, WaterClass::Invalid, 0);
	world.company->location_of_HQ = world.tile;
	const auto relocation = NativeData<Commands::BuildObject>(TileXY(30, 30), ObjectType(OBJECT_HQ), uint8_t{0});
	const auto before = ConstructionIntentState(Commands::BuildObject, relocation);
	REQUIRE(before.has_value());
	const auto footprint = ConstructionIntentTiles(Commands::BuildObject, relocation);
	REQUIRE(footprint.has_value());
	CHECK(footprint->size() == 8);
	CHECK(std::ranges::find(*footprint, world.tile + TileDiffXY(1, 1)) != footprint->end());
	CHECK(std::ranges::find(*footprint, TileXY(31, 31)) != footprint->end());
	world.company->location_of_HQ = INVALID_TILE;
	CHECK(ConstructionIntentState(Commands::BuildObject, relocation) != before);

	MakeRoadNormal(world.tile, ROAD_X, ROADTYPE_ROAD, INVALID_ROADTYPE, TownID::Invalid(), _current_company, OWNER_NONE);
	const auto road = NativeData<Commands::BuildRoad>(world.tile, ROAD_X, ROADTYPE_ROAD, DisallowedRoadDirections(DisallowedRoadDirection::Northbound), TownID::Invalid());
	const auto two_way = ConstructionIntentState(Commands::BuildRoad, road);
	SetDisallowedRoadDirections(world.tile, DisallowedRoadDirections(DisallowedRoadDirection::Southbound));
	CHECK(ConstructionIntentState(Commands::BuildRoad, road) != two_way);
}

TEST_CASE("Selected-tile control references and build acknowledgements retain native asset ownership", "[integration][control-reference]")
{
	ConstructionWorld world;
	REQUIRE(Depot::CanAllocateItem());
	Depot *depot = Depot::Create(world.tile);
	MakeRoadDepot(world.tile, _current_company, depot->index, DiagDirection::NE, ROADTYPE_ROAD);
	const Json depot_action = {{"name", "road.depot.build"}, {"parameters", {{"tile", world.tile.base()}, {"direction", 0}}}};
	CHECK(Integration::ResolveControlReference({{"kind", "depot"}, {"tile", world.tile.base()}}) ==
		Json{{"kind", "depot"}, {"reference", Handle(depot)}});
	CHECK(Integration::FormatCommandResult(Commands::BuildRoadDepot, CommandCost(), {}, depot_action).at("depot_id") == Handle(depot));
	CHECK_FALSE(Integration::FormatCommandResult(Commands::BuildRoadDepot, CommandCost(INVALID_STRING_ID), {}, depot_action).contains("depot_id"));
	SetTileOwner(world.tile, OWNER_NONE);
	CHECK_THROWS_AS(Integration::ResolveControlReference({{"kind", "depot"}, {"tile", world.tile.base()}}), InvalidParameters);
	CHECK_FALSE(Integration::FormatCommandResult(Commands::BuildRoadDepot, CommandCost(), {}, depot_action).contains("depot_id"));

	const TileIndex stop_tile = TileXY(24, 24);
	REQUIRE(Station::CanAllocateItem());
	Station *station = Station::Create(stop_tile);
	station->owner = _current_company;
	MakeRoadStop(stop_tile, _current_company, station->index, RoadStopType::Bus, ROADTYPE_ROAD, INVALID_ROADTYPE, DiagDirection::NE);
	const Json stop_action = {{"name", "road.stop.build"}, {"parameters", {{"tile", stop_tile.base()}, {"direction", 0}, {"stop_type", "bus"}}}};
	CHECK(Integration::ResolveControlReference({{"kind", "station"}, {"tile", stop_tile.base()}}).at("reference") == Handle(station));
	CHECK(Integration::FormatCommandResult(Commands::BuildRoadStop, CommandCost(), {}, stop_action).at("station_id") == Handle(station));
	CHECK_THROWS_AS(Integration::ResolveControlReference({{"kind", "waypoint"}, {"tile", stop_tile.base()}}), InvalidParameters);
	REQUIRE(Company::CanAllocateItem());
	station->owner = Company::Create()->index;
	CHECK_THROWS_AS(Integration::ResolveControlReference({{"kind", "station"}, {"tile", stop_tile.base()}}), InvalidParameters);
	CHECK_FALSE(Integration::FormatCommandResult(Commands::BuildRoadStop, CommandCost(), {}, stop_action).contains("station_id"));
}
