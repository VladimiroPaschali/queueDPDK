# Analisi queue switching: FPGA/QDMA vs Mellanox MLX5

## Contesto

L'applicazione (`asni-nat`) gira su un singolo core e cicla su N code RX
per distribuire meglio il traffico. Il loop principale è in `src/main.c:1847`:

```c
for (i = 0; i < queues; ++i) {
    nb_rx = rte_eth_rx_burst(portid, i, pkts_burst, MAX_PKT_BURST);
    // processa pacchetti...
}
```

Il punto di partenza comune a tutti e tre i driver è `rte_ethdev.h:4849`:

```c
nb_rx = (*dev->rx_pkt_burst)(dev->data->rx_queues[queue_id], rx_pkts, nb_pkts);
```

`dev->data->rx_queues` è un array di `void*`. Ogni cambio di `queue_id`
carica un puntatore diverso e quindi una struttura dati diversa in cache.
Quello è il punto critico da analizzare.

---

## Driver 1 — QDMA/FPGA (`dpdk-20.11`)

### Struttura `qdma_rx_queue` (`drivers/net/qdma/qdma.h:167`, ~1100 byte)

```c
struct qdma_rx_queue {
    struct rte_mempool  *mb_pool;
    void                *rx_ring;
    union qdma_ul_st_cmpt_ring *cmpt_ring;
    struct wb_status    *wb_status;       // puntatore a write-back HW
    struct rte_mbuf    **sw_ring;
    struct rte_eth_dev  *dev;

    uint16_t rx_tail, cmpt_desc_len, rx_buff_size, nb_rx_desc, nb_rx_cmpt_desc;
    uint32_t queue_id;
    uint64_t mbuf_initializer;

    struct qdma_q_pidx_reg_info      q_pidx_info;
    struct qdma_q_cmpt_cidx_reg_info cmpt_cidx_info;  // contiene wrb_cidx
    struct qdma_pkt_stats            stats;

    // --- IL CAMPO CHIAVE ---
    union qdma_ul_st_cmpt_ring cmpt_data[128];  // 128 x 8 = 1024 byte EMBEDDED
    // ...
};
```

`rx_queues[q]` punta direttamente a questa struct (`qdma_devops.c:623`):

```c
dev->data->rx_queues[rx_queue_id] = rxq;
```

### Hot path (`qdma_rxtx.c:891`)

```c
wb_status    = rxq->wb_status;              // 1 dereference → HW write-back memory
cmpt_pidx    = wb_status->pidx;             // legge pidx dall'HW
rx_cmpt_tail = rxq->cmpt_cidx_info.wrb_cidx; // IN-STRUCT, già in cache
nb_pkts_avail = cmpt_pidx - rx_cmpt_tail;  // sa subito se la coda è vuota
// poi legge da rxq->cmpt_data[] — EMBEDDED, in cache insieme al resto
```

**Coda vuota?** → confronto tra `wb_status->pidx` (1 dereference a HW) e
`wrb_cidx` (in-struct). Zero accessi a memoria DMA separata per decidere
se c'è lavoro.

---

## Driver 2 — MLX5 (`dpdk-20.11-original`)

### Struttura `mlx5_rxq_data` (`drivers/net/mlx5/mlx5_rxtx.h:111`, ~1100 byte, `__rte_cache_aligned`)

```c
struct mlx5_rxq_data {
    unsigned int csum:1; ...              // bitfields di config (~4 byte)

    volatile uint32_t *rq_db;            // puntatore a doorbell (regione DMA separata)
    volatile uint32_t *cq_db;            // puntatore a doorbell (regione DMA separata)
    uint16_t port_id;
    uint32_t elts_ci, rq_ci, rq_pi, cq_ci;  // contatori hot (~20 byte)
    union { struct rxq_zip zip; uint16_t decompressed; };

    struct mlx5_mr_ctrl mr_ctrl;         // 184 byte embedded — working set SEPARATO

    volatile void              *wqes;    // puntatore a WQ ring (regione DMA separata)
    volatile struct mlx5_cqe (*cqes)[]; // puntatore a CQ ring (regione DMA separata)
    struct rte_mbuf          *(*elts)[]; // puntatore ad array mbuf (heap separato)
    struct mlx5_dev_ctx_shared *sh;      // puntatore a contesto device condiviso (enorme)
    // ...
    struct rte_mbuf fake_mbuf;           // 232 byte embedded — FREDDO (padding vectorized RX)
    // ...
    struct mlx5_eth_rxseg rxseg[32];     // 32 x 16 = 512 byte embedded — FREDDO (config statica)
} __rte_cache_aligned;
```

`rx_queues[q]` punta a `&rxq_ctrl->rxq` — la `mlx5_rxq_data` è il primo
campo di `mlx5_rxq_ctrl` (`mlx5_rxq.c:743`).

### Hot path (`mlx5_rxtx.c:1387`)

```c
struct mlx5_rxq_data *rxq = dpdk_rxq;
const unsigned int cqe_cnt = (1 << rxq->cqe_n) - 1;          // bitfield, cache line 0

volatile struct mlx5_cqe *cqe =
    &(*rxq->cqes)[rxq->cq_ci & cqe_cnt];                      // segui rxq->cqes → CQ ring DMA

struct rte_mbuf *rep = (*rxq->elts)[idx];                      // segui rxq->elts → array mbuf

volatile struct mlx5_wqe_data_seg *wqe =
    &((volatile struct mlx5_wqe_data_seg *)rxq->wqes)[idx];   // segui rxq->wqes → WQ ring DMA
```

**Coda vuota?** → bisogna seguire `rxq->cqes` e leggere il byte di ownership
del CQE dalla memoria DMA — un accesso a una regione allocata separatamente,
fuori dalla struct.

---

## Driver 3 — MLX5 (`dpdk-25.11`)

Stesso problema strutturale, struct leggermente più grande (~1088 byte), con:

- **Nuovo dispatch**: `rte_eth_fp_ops` (struttura piatta e cache-aligned) elimina
  2-3 cache miss sul lookup della coda rispetto a `rte_eth_devices[port]→data→rx_queues[q]`.
  Migliora il dispatch ma non il contenuto della struttura per-coda.
- `mr_ctrl` spostato a offset 72 invece di ~145 — piccolo miglioramento di
  località, ma ancora embedded.
- `rxseg[32]` (512 B) e `fake_mbuf` (232 B) ancora embedded — problema non risolto.
- Aggiunta di `mlx5_rxq_priv` separato dal data path, che apre la strada alla
  Shared RX Queue (SRQ): più code che condividono una sola `mlx5_rxq_data`.
  Con SRQ configurata esplicitamente il problema del cache miss per-coda
  potrebbe ridursi sensibilmente.

---

## Confronto diretto

| Aspetto | QDMA/FPGA | MLX5 (20.11 e 25.11) |
|---------|-----------|----------------------|
| Puntatori da seguire per burst | 1 (`wb_status→pidx`) | 4+ (`cqes`, `wqes`, `elts`, `rq_db`, `cq_db`) |
| Destinazione dei puntatori | 1 regione HW write-back (spesso condivisa) | 5 regioni DMA allocate separatamente per coda |
| Costo "coda vuota?" | confronto in-struct | dereference a memoria DMA → cache miss |
| Dato caldo embedded | `cmpt_data[128]` = completions dentro la struct | `cqes[]` è un puntatore fuori dalla struct |
| Dati freddi nella struct | quasi nulla oltre `cmpt_data` | 512 B `rxseg[]` + 232 B `fake_mbuf` + 184 B `mr_ctrl` |
| Cache line utili al cambio coda | ~17, quasi tutte utili | ~17, di cui ~12 fredde |

---

## Causa principale del gap di performance

Con N code sullo stesso core, il loop controlla ogni coda per vedere se ha
pacchetti. Quando il traffico è distribuito, molte iterazioni saranno
"coda vuota".

**QDMA**: "coda vuota" costa 1 miss su `wb_status` (piccola struttura HW
write-back, spesso già in cache) più un confronto in-struct. ~1-2 miss per coda.

**MLX5**: "coda vuota" richiede di seguire `rxq->cqes` e leggere l'ownership
bit del CQE da una regione DMA allocata separatamente. Con N code, sono N
accessi a N regioni DMA distinte. ~3-4 miss per coda vuota.

Con la configurazione a 16 code (`-q 16`), se solo 1-2 code hanno traffico,
MLX5 esegue ~56 cache miss solo per scoprire che 14 code sono vuote, contro
~14 miss di QDMA.

Il guadagno delle code sull'FPGA viene esattamente da questo: distribuire
il traffico in burst naturali riduce la contention sulla singola coda, e il
check "è vuota?" è economico perché il dato di stato è embedded nella struct
stessa. Su MLX5 il check è costoso indipendentemente dal risultato, perché
richiede di seguire un puntatore verso la memoria DMA della NIC.

---

## Possibili ottimizzazioni per MLX5

1. **Prefetch esplicito**: nel loop, prefetchare `rxq->cqes` della coda
   successiva mentre si processa quella corrente.

2. **Batch per coda**: raccogliere più burst sulla stessa coda prima di
   switchare, per ammortizzare il costo del miss iniziale.

3. **Shared RX Queue (DPDK 25.11)**: configurare le code in modalità SRQ
   in modo che condividano la stessa `mlx5_rxq_data` — il dato rimane in
   cache attraverso i cambi di coda.

4. **Ridurre il numero di code attive**: su MLX5 poche code larghe performano
   meglio di tante code strette, al contrario dell'FPGA.
