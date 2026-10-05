#include "nat_flowmanager.h"
#include <stdio.h>

struct FlowManager {
	struct State  *state;
	uint32_t       expiration_time; /*nanoseconds*/
	bool           shared;          /* Shared: lock every access, 15-byte FlowId entries */
	rte_spinlock_t lock;
};

static inline void
flow_manager_lock(struct FlowManager *manager)
{
	if (manager->shared)
		rte_spinlock_lock(&manager->lock);
}

static inline void
flow_manager_unlock(struct FlowManager *manager)
{
	if (manager->shared)
		rte_spinlock_unlock(&manager->lock);
}

static inline void
flowid_copy(const struct FlowManager *manager, struct FlowId *dst, const struct FlowId *src)
{
	if (manager->shared)
		memcpy((void *)dst, (const void *)src, FLOWID_PACKED_SIZE);
	else
		memcpy((void *)dst, (const void *)src, sizeof(struct FlowId));
}

struct FlowManager *
flow_manager_allocate(uint16_t starting_port,
                      uint32_t nat_ip,
                      uint16_t nat_device,
                      uint32_t expiration_time,
                      uint64_t max_flows,
                      bool     shared)
{
	struct FlowManager *manager = (struct FlowManager *)malloc(sizeof(struct FlowManager));
	if (manager == NULL) {
		return NULL;
	}
	manager->state = alloc_state(max_flows,
	                             starting_port,
	                             nat_ip,
	                             nat_device,
	                             shared ? FLOWID_PACKED_SIZE : sizeof(struct FlowId));
	if (manager->state == NULL) {
		return NULL;
	}

	manager->expiration_time = expiration_time;
	manager->shared          = shared;
	rte_spinlock_init(&manager->lock);

	return manager;
}

bool
flow_manager_allocate_flow(struct FlowManager *manager,
                           struct FlowId      *id,
                           uint16_t            internal_device,
                           vigor_time_t        time,
                           uint16_t           *external_port)
{
	UNUSED(internal_device);
	int index;

	flow_manager_lock(manager);
	if (dchain_allocate_new_index(manager->state->heap, &index, time) == 0) {
		flow_manager_unlock(manager);
		return false;
	}

	*external_port = manager->state->start_port + index;

	struct FlowId *key = 0;
	vector_borrow(manager->state->fv, index, (void **)&key);
	flowid_copy(manager, key, id);
	map_put(manager->state->fm, key, index);
	vector_return(manager->state->fv, index, key);
	flow_manager_unlock(manager);
	return true;
}

void
flow_manager_expire(struct FlowManager *manager, vigor_time_t time)
{
	assert(time >= 0); // we don't support the past
	assert(sizeof(vigor_time_t) <= sizeof(uint64_t));
	uint64_t     time_u    = (uint64_t)time; // OK because of the two asserts
	vigor_time_t last_time = time_u - manager->expiration_time * 1000; // convert us to ns

	flow_manager_lock(manager);
	expire_items_single_map(
	    manager->state->heap, manager->state->fv, manager->state->fm, last_time);
	flow_manager_unlock(manager);
}

bool
flow_manager_get_internal(struct FlowManager *manager,
                          struct FlowId      *id,
                          vigor_time_t        time,
                          uint16_t           *external_port)
{
	int index;

	flow_manager_lock(manager);
	if (map_get(manager->state->fm, id, &index) == 0) {
		flow_manager_unlock(manager);
		return false;
	}
	*external_port = index + manager->state->start_port;
	dchain_rejuvenate_index(manager->state->heap, index, time);
	flow_manager_unlock(manager);
	return true;
}

bool
flow_manager_get_external(struct FlowManager *manager,
                          uint16_t            external_port,
                          vigor_time_t        time,
                          struct FlowId      *out_flow)
{
	int index = external_port - manager->state->start_port;
	if (dchain_is_index_allocated(manager->state->heap, index) == 0) {
		return false;
	}

	flow_manager_lock(manager);
	struct FlowId *key = 0;
	vector_borrow(manager->state->fv, index, (void **)&key);
	flowid_copy(manager, out_flow, key);
	vector_return(manager->state->fv, index, key);

	dchain_rejuvenate_index(manager->state->heap, index, time);
	flow_manager_unlock(manager);

	return true;
}
