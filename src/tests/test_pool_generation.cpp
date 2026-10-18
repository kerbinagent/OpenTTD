/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 * OpenTTD is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
 * See the GNU General Public License for more details. You should have received a copy of the GNU General Public License along with OpenTTD. If not, see <https://www.gnu.org/licenses/old-licenses/gpl-2.0>.
 */

/** @file test_pool_generation.cpp Tests for identities across pool item lifetimes. */

#include "../stdafx.h"

#include "../3rdparty/catch2/catch.hpp"
#include "../core/pool_func.hpp"

#include "../safeguards.h"

namespace {

using GenerationTestID = PoolID<uint16_t, struct GenerationTestIDTag, 64, UINT16_MAX>;

struct GenerationTestItem;
struct CachedGenerationTestItem;
using GenerationTestPool = Pool<GenerationTestItem, GenerationTestID, 4, PoolType::Data>;
using CachedGenerationTestPool = Pool<CachedGenerationTestItem, GenerationTestID, 4, PoolType::Data, true>;

extern GenerationTestPool _generation_test_pool;
extern CachedGenerationTestPool _cached_generation_test_pool;

struct GenerationTestItem : GenerationTestPool::PoolItem<&_generation_test_pool> {
	using Base = GenerationTestPool::PoolItem<&_generation_test_pool>;
	GenerationTestItem(GenerationTestID index) : Base(index) {}
	static GenerationTestPool &GetTestPool() { return _generation_test_pool; }
};

struct CachedGenerationTestItem : CachedGenerationTestPool::PoolItem<&_cached_generation_test_pool> {
	using Base = CachedGenerationTestPool::PoolItem<&_cached_generation_test_pool>;
	CachedGenerationTestItem(GenerationTestID index) : Base(index) {}
	static CachedGenerationTestPool &GetTestPool() { return _cached_generation_test_pool; }
};

static_assert(std::is_standard_layout_v<GenerationTestItem::Base>);
static_assert(std::is_trivially_copy_constructible_v<GenerationTestItem::Base>);
static_assert(std::is_trivially_move_constructible_v<GenerationTestItem::Base>);

GenerationTestPool _generation_test_pool("GenerationTestItem");
CachedGenerationTestPool _cached_generation_test_pool("CachedGenerationTestItem");

template <typename T>
struct GenerationTestFixture {
	GenerationTestFixture() { T::GetTestPool().CleanPool(); }
	~GenerationTestFixture() { T::GetTestPool().CleanPool(); }
};

} // namespace

TEMPLATE_TEST_CASE("Pool - generations distinguish reused indices", "[pool]", GenerationTestItem, CachedGenerationTestItem)
{
	GenerationTestFixture<TestType> fixture;
	REQUIRE(TestType::CanAllocateItem(2));
	auto *first = TestType::Create();
	const auto first_index = first->index;
	const auto first_generation = first->GetInstanceGeneration();
	auto *second = TestType::Create();
	const auto second_generation = second->GetInstanceGeneration();

	CHECK(first_generation != 0);
	CHECK(second->index != first_index);
	CHECK(second_generation > first_generation);
	delete first;

	REQUIRE(TestType::CanAllocateItem());
	auto *replacement = TestType::Create();
	CHECK(replacement->index == first_index);
	CHECK(replacement->GetInstanceGeneration() > second_generation);
	CHECK(second->GetInstanceGeneration() == second_generation);
}

TEMPLATE_TEST_CASE("Pool - clearing and explicit-index loading preserve generation history", "[pool]", GenerationTestItem, CachedGenerationTestItem)
{
	GenerationTestFixture<TestType> fixture;
	const GenerationTestID index{17};
	auto *original = TestType::CreateAtIndex(index);
	const auto original_generation = original->GetInstanceGeneration();
	TestType::GetTestPool().CleanPool();

	CHECK(TestType::GetNumItems() == 0);
	CHECK_FALSE(TestType::IsValidID(index));
	auto *loaded = TestType::CreateAtIndex(index);
	const auto loaded_generation = loaded->GetInstanceGeneration();
	CHECK(loaded->index == index);
	CHECK(loaded_generation > original_generation);
	TestType::GetTestPool().CleanPool();

	auto *new_world = TestType::CreateAtIndex(index);
	CHECK(new_world->GetInstanceGeneration() > loaded_generation);
}

TEMPLATE_TEST_CASE("Pool - copied snapshots retain their generation", "[pool]", GenerationTestItem, CachedGenerationTestItem)
{
	GenerationTestFixture<TestType> fixture;
	REQUIRE(TestType::CanAllocateItem(2));
	auto *original = TestType::Create();
	const auto generation = original->GetInstanceGeneration();
	TestType copy(*original);
	TestType moved(std::move(copy));

	CHECK(moved.index == original->index);
	CHECK(copy.GetInstanceGeneration() == generation);
	CHECK(moved.GetInstanceGeneration() == generation);
	auto *next = TestType::Create();
	CHECK(next->GetInstanceGeneration() == generation + 1);
}
