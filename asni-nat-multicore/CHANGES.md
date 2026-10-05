# Modifiche per ottimizzazione multicore

## Problema
Con 1 coda per core le performance erano buone (~12 Mpps).
Con 2+ code per core le performance calavano drasticamente.
Causa principale: false sharing, pressure sulla L1D cache, pointer indirection nella CMS.

---

## src/main.c

### 1. Count-Min Sketch — eliminazione doppia indirezione
**Prima:**
```c
#define HASHFN_N 16
#define COLUMNS  1048576

struct countmin {
    uint64_t **values;   // puntatore a array di puntatori
};

// Allocazione: 16+1 rte_zmalloc separati per riga
cm->values = rte_zmalloc(..., sizeof(uint64_t*) * HASHFN_N, ...);
for (i = 0; i < HASHFN_N; i++)
    cm->values[i] = rte_zmalloc(..., sizeof(uint64_t) * COLUMNS, ...);
```
**Dopo:**
```c
#define HASHFN_N 8
#define COLUMNS  4096

struct countmin {
    uint64_t values[HASHFN_N][COLUMNS];   // array 2D embedded
};

// Allocazione: singolo rte_zmalloc_socket per core
cm_per_core[lcore_id] = rte_zmalloc_socket(NULL, sizeof(struct countmin), 64,
                                            rte_lcore_to_socket_id(lcore_id));
```
**Perché:** ogni accesso alla CMS faceva prima un load del puntatore alla riga,
poi un secondo load al valore. Questo generava "load blocked by address" per il
47-59% di tutti i load (visibile con perf c2c). Con l'array embedded si elimina
il doppio salto. Dimensione: da ~8 MB/core (non stava in cache) a 256 KB/core
(sta in L2). Allocazione NUMA-aware per evitare accessi cross-socket.

---

### 2. Pool mbuf per lcore invece che per coda
**Prima:** un pool per coda (o shared), `pktmbuf_pool[1024]`.
**Dopo:** un pool per lcore, `pktmbuf_pool[RTE_MAX_LCORE]`, allocato con
`rte_lcore_to_socket_id` per NUMA locality. Ogni coda di un lcore usa lo stesso
pool, riducendo la footprint in cache del free-list del mempool.

---

### 3. TX batching
**Prima:** `rte_eth_tx_burst` chiamato dentro il loop per ogni coda RX.
**Dopo:** i pacchetti di tutte le code vengono accumulati in `tx_batch[512]` e
trasmessi con un singolo `rte_eth_tx_burst` alla fine dell'iterazione del loop.
Amortizza l'overhead fisso del TX (sfence + MMIO write PIDX) su tutti i pacchetti
dell'iterazione invece di pagarlo per ogni coda.

Nota: non ha avuto impatto misurabile sulle performance (il bottleneck era in RX).

---

### 4. Fix indice relativo in queue_hit_local
**Prima:** `queue_hit_local[i]` con `i` assoluto (0..queues-1),
causava accessi fuori bounds per lcore che partivano da queue_start > 0.
**Dopo:** `queue_hit_local[qi]` con `qi = i - queue_start` (indice relativo).

---

## dpdk-20.11/drivers/net/qdma/qdma.h

### 5. MIN_RX_PIDX_UPDATE_THRESHOLD: 1 → 32
**Prima:** `rearm_c2h_ring` (sfence + MMIO write PIDX) veniva chiamato ad ogni
burst, anche per 1 solo pacchetto.
**Dopo:** viene saltato se meno di 32 descrittori sono pending. Riduce le
scritture MMIO su PCIe (~200-500 ns ciascuna) nei burst piccoli.

---

## dpdk-20.11/drivers/net/qdma/qdma_rxtx.c

### 6. prepare_packets — fast path per pacchetti non-segmentati
**Prima:** chiamava sempre `prepare_segmented_packet` per ogni pacchetto,
anche per pacchetti normali (≤ rx_buff_size). Questa funzione ha un loop
do-while, gestione linked list (first_seg/last_seg), e legge/scrive
`rxq->rx_tail` tramite puntatore ad ogni pacchetto (32 load + 32 store per burst).

**Dopo:** fast path inline con `likely(pkt_length <= rxq->rx_buff_size)`:
- `id` (rx_tail) tenuto in registro per tutta la durata del burst
- Imposta i 7 campi dell'mbuf direttamente senza overhead di linked list
- Solo 1 load + 1 store su `rxq->rx_tail` per burst invece di 32+32
- Fallback a `prepare_segmented_packet` solo per pacchetti jumbo (raro)

---

### 7. Prefetch di wb_status alla fine del burst
**Prima:** alla fine di `qdma_recv_pkts_st` nessun prefetch.
**Dopo:**
```c
rte_prefetch0(rxq->wb_status);
```
`wb_status->pidx` è scritto dall'FPGA via DMA (PCIe), quindi ogni lettura è
una miss da coerenza della cache (~200-400 cicli). Con il prefetch alla fine
del burst, quella miss viene avviata mentre il core processa le altre code nel
loop. Quando il loop ritorna su questa coda, `wb_status` è già in L1 cache.

---

## Modifiche tentate e rivertite

### CMPT CIDX batching (revertito)
Tentativo di ridurre le scritture MMIO per il CIDX del completion ring
(MIN_CMPT_CIDX_UPDATE_THRESHOLD). Rotto: ad alto traffico l'FPGA riempie il
cmpt_ring (1024 entry) prima che il CPU raggiunga la soglia, poi stalla e
smette di mandare completions → pacchetti si bloccano dopo qualche secondo.

### Riduzione nb_rxd/nb_txd a 256 (revertito su richiesta)
Riduzione dei ring da 1024 a 256 descrittori. Avrebbe ridotto il footprint
per coda da ~25 KB a ~6.5 KB, permettendo a 4 code di stare in L1D.
Revertito perché il numero di descrittori deve restare al valore originale.

### QDMA_MAX_BURST_SIZE 128 → 64/32 (revertito su richiesta)
Burst size ridotto per dimezzare le iterazioni del loop e le MMIO write.
Revertito perché il burst size deve restare al valore originale.
