#include "nat_flowmanager.h"
#include <stdio.h>

struct FlowManager {
	struct State *state;
	uint32_t      expiration_time; /*nanoseconds*/
	rte_spinlock_t lock;
};

struct FlowManager *
flow_manager_allocate(uint16_t starting_port,
                      uint32_t nat_ip,
                      uint16_t nat_device,
                      uint32_t expiration_time,
                      uint64_t max_flows)
{
	struct FlowManager *manager = (struct FlowManager *)malloc(sizeof(struct FlowManager));
	if (manager == NULL) {
		return NULL;
	}
	manager->state = alloc_state(max_flows, starting_port, nat_ip, nat_device);
	if (manager->state == NULL) {
		return NULL;
	}

	manager->expiration_time = expiration_time;
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
	
	if (manager == NULL || manager->state == NULL || manager->state->fv == NULL) {
		return false;
	}
	
	int index;
	
	rte_spinlock_lock(&manager->lock);
	if (dchain_allocate_new_index(manager->state->heap, &index, time) == 0) {
		rte_spinlock_unlock(&manager->lock);
		return false;
	}

	*external_port = manager->state->start_port + index;

	struct FlowId *key = 0;
	vector_borrow(manager->state->fv, index, (void **)&key);
	
	if (key == NULL) {
		rte_spinlock_unlock(&manager->lock);
		return false;
	}
	
	if (id == NULL) {
		rte_spinlock_unlock(&manager->lock);
		return false;
	}
	
	memcpy((void *)key, (void *)id, sizeof(struct FlowId));
	map_put(manager->state->fm, key, index);
	vector_return(manager->state->fv, index, key);
	rte_spinlock_unlock(&manager->lock);
	
	return true;
}

void
flow_manager_expire(struct FlowManager *manager, vigor_time_t time)
{
	assert(time >= 0);
	assert(sizeof(vigor_time_t) <= sizeof(uint64_t));
	uint64_t     time_u    = (uint64_t)time;
	vigor_time_t last_time = time_u - manager->expiration_time * 1000;
	
	rte_spinlock_lock(&manager->lock);
	expire_items_single_map(
	    manager->state->heap, manager->state->fv, manager->state->fm, last_time);
	rte_spinlock_unlock(&manager->lock);
}

bool
flow_manager_get_internal(struct FlowManager *manager,
                          struct FlowId      *id,
                          vigor_time_t        time,
                          uint16_t           *external_port)
{
	int index;
	
	rte_spinlock_lock(&manager->lock);
	if (map_get(manager->state->fm, id, &index) == 0) {
		rte_spinlock_unlock(&manager->lock);
		return false;
	}
	*external_port = index + manager->state->start_port;
	dchain_rejuvenate_index(manager->state->heap, index, time);
	rte_spinlock_unlock(&manager->lock);
	
	return true;
}

bool
flow_manager_get_external(struct FlowManager *manager,
                          uint16_t            external_port,
                          vigor_time_t        time,
                          struct FlowId      *out_flow)
{
	if (manager == NULL || manager->state == NULL || manager->state->fv == NULL) {
		return false;
	}
	
	int index = external_port - manager->state->start_port;
	if (dchain_is_index_allocated(manager->state->heap, index) == 0) {
		return false;
	}

	rte_spinlock_lock(&manager->lock);
	struct FlowId *key = 0;
	vector_borrow(manager->state->fv, index, (void **)&key);
	
	if (key == NULL) {
		rte_spinlock_unlock(&manager->lock);
		return false;
	}
	
	memcpy((void *)out_flow, (void *)key, sizeof(struct FlowId));
	vector_return(manager->state->fv, index, key);

	dchain_rejuvenate_index(manager->state->heap, index, time);
	rte_spinlock_unlock(&manager->lock);

	return true;
}
