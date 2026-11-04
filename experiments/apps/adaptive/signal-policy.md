# Controllore adattivo `--adaptive`

Questo documento descrive il controllore che sceglie il numero di code RX attive guardando il
backlog dei ring (`--adaptive`). Descrive poi i risultati delle campagne
`adaptive_ndr.py` (`results/adaptive/ndr_v2/`) e `adaptive.py` (`results/adaptive/timeline/`).
Il codice è in `src/adaptive.h` (`adaptive_epoch`) e in `src/main.c`
(`set_active_queues`, `poll_loop`), oltre a `src/qdma_rss.c`.

## 1. Code RSS: cosa cambia davvero

All'avvio l'app configura tutte le `-q` code, che devono essere una potenza di 2. La NIC (Open-NIC)
manda un pacchetto alla coda `hash & mask`, dove `mask` è un registro a 16 bit: la prima entry
della tabella di indirezione, la stessa che scrive `qdma_dev_reta_update` patchato. Per cambiare
il numero di code basta scrivere `n-1` in quel registro. I ring DPDK non vengono toccati.

- **Indirizzo**: `qdma_rss_mask_reg()` lo calcola come il PMD,
  `QDMA_FUNC_OFFSET_INDIR_TABLE(func_id, 0)`, con il `func_id` letto dalla struttura privata
  del driver. Non si legge né si scrive nessun altro indirizzo del BAR utente.
- **Verifica all'avvio** (`rss_mask_locate`):
  1. legge il registro due volte e tiene i 16 bit bassi (la prima lettura dopo l'avvio dà
     `0xdeadbeff`);
  2. controlla che contenga `queues-1`;
  3. scrive `queues/2-1` e controlla che `rte_eth_dev_rss_reta_query` lo veda;
  4. ripristina il valore originale.

  Se una verifica fallisce, l'app esce.

Il loop usa due contatori:

| variabile | significato |
|
| `active_queues` | code su cui la NIC distribuisce (registro = `active_queues-1`) |
| `polled_queues` | code interrogate dal loop, sempre `>= active_queues` |

- **Crescita**: alza prima `polled_queues` e `active_queues`, poi scrive il registro. Le code
  nuove vengono quindi lette prima che la NIC ci metta pacchetti. Il loro `queue_hit[q]` parte
  da `penalty`, così Penalty non le salta al primo giro.
- **Riduzione**: scrive prima il registro, poi abbassa `active_queues` e imposta
  `drain_until = now + 200 µs`. Le code tolte (`[active, polled)`) restano nel giro in *drain*:
  - vengono lette a ogni sweep, senza Penalty né Aggressive;
  - non contano nelle statistiche del controllore.

  Quando uno sweep le trova tutte vuote e sono passati 200 µs, `polled_queues = active_queues`.
  Nessun pacchetto resta in un ring che nessuno legge. Un flusso che cambia coda può essere
  riordinato una volta, tra il backlog sulla coda vecchia e i pacchetti nuovi.

Il numero di code si muove solo per potenze di 2, un raddoppio o un dimezzamento per decisione
(`cur_log ± 1`), tra `--adaptive-min` e `-q`, partendo da `--adaptive-start` (default `-q`).

## 2. Cosa si misura nel loop

### Backlog di una coda

`qdma_rx_backlog()` calcola in O(1) i pacchetti che aspettano nel ring:

```
pidx = rxq->wb_status->pidx            // producer index scritto dall'hardware (write-back)
tail = rxq->cmpt_cidx_info.wrb_cidx    // consumer index del driver sul completion ring
backlog = tail <= pidx ? pidx - tail : nb_rx_cmpt_desc - 1 - tail + pidx
```

È lo stesso conto di `rx_queue_count()` del PMD, ma senza scorrere i descrittori.

### Quando viene letto

Solo **dopo un poll di una coda attiva che restituisce un burst pieno** (`nb_rx == 32`). Un burst
non pieno vuol dire che il ring è stato svuotato e il backlog è 0, quindi non serve leggerlo.
Per ogni burst pieno:

- `sweep_full++`
- `sweep_half++` se `2*backlog > rx_ring_size`: dopo il burst aspetta ancora più di metà ring.

Contano anche `sweep_polls` (poll delle code attive) e `sweep_empty` (poll che non leggono
niente). Le esclusioni:

- le code in drain non contano;
- i salti di Penalty non contano, perché non sono poll;
- i ripoll di Aggressive contano come poll separati.

### Solo gli sweep con lavoro

All'inizio di ogni sweep i contatori dello sweep precedente si sommano a quelli dell'epoca
**solo se quello sweep ha letto almeno un pacchetto**. Vale anche per i cicli usati nel costo per
pacchetto. Un core inattivo che gira su code vuote non spreca niente e non deve spingere a ridurre
le code.

### Le tre grandezze dell'epoca

| grandezza | formula | uso |
|---|
| **backlog share** `s` | `epoch_half / epoch_full` (0 se non ci sono burst pieni) | segnale principale |
| **empty share** `e` | `epoch_empty / epoch_polls` | segnale di troppe code |
| **costo** `c` | cicli degli sweep con lavoro / pacchetti | solo per giudicare una crescita |

#### Come leggere la backlog share

Nella definizione ci sono due soglie diverse, da non confondere:

- **"metà ring"** riguarda *quanto è pieno il ring in un dato momento*. È la soglia su ogni
  singola lettura del backlog: dopo un burst pieno, la coda ha ancora più di `rx_ring_size/2`
  pacchetti in attesa (più di 512 con il ring da 1024)?
- **`s`** è *quante volte succede*: la percentuale dei burst pieni dell'epoca per cui la risposta
  è sì.

Quindi `s = 5 %` **non** vuol dire che il ring è pieno a metà o pieno il 5 % del tempo. Vuol dire
che 1 burst pieno su 20 ha trovato, dopo essersi preso 32 pacchetti, più di mezzo ring ancora in
coda. Il denominatore sono solo i burst pieni, non tutti i poll né il tempo. Un'epoca senza burst
pieni ha `s = 0`.

Perché questa misura indica la perdita: la NIC scarta un pacchetto solo quando il ring è pieno.
Un ring oltre metà dopo che il core lo ha appena servito vuol dire che il core non tiene il passo
su quella coda e che il ring è vicino a riempirsi. Se capita di rado (un picco momentaneo), il
core recupera. Se capita spesso, prima o poi il ring si riempie e la NIC scarta.

Il backlog massimo non funziona come segnale: arriva a ring pieno (96.7 % nei log) anche senza
perdite, basta un solo picco. Anche i burst pieni da soli non sono un segnale, perché Aggressive
smaltisce un backlog proprio con burst pieni consecutivi senza perdere niente.

#### Da dove vengono 5 % e 0.5 %

Le soglie sono empiriche. `experiments/adaptive_calib.py` misura `s` e la perdita per 1-2048 code
fisse, le tre policy e rate da 1 a 3.6 Mpps (126 punti, `results/adaptive/calib.csv`).

| `s` misurata | punti | con perdita > 0.1 % | perdita massima |
|---|---|
| < 0.5 % | 61 | 0 | 0.008 % |
| 0.5-1 % | 1 | 0 | 0.002 % |
| 1-5 % | 7 | 3 | 1.35 % |
| 5-10 % | 4 | 3 | 3.1 % |
| 10-30 % | 2 | 2 | 8.5 % |
| > 30 % | 51 | 51 | 25 % |

- Sotto 1 % non c'è mai perdita.
- Sopra 5 % quasi sempre: 56 punti su 57. L'eccezione è Aggressive a 16 code a 3 Mpps
  (`s` = 9.7 %, nessuna perdita).
- Tra 1 e 5 % i punti sono pochi e misti.

Da qui le soglie:
- **`H` = 5 %**: oltre questo valore la perdita è quasi certa, quindi si raddoppia;
- **`L` = 0.5 %**: sotto questo valore non c'è mai stata perdita, quindi si può provare con meno
  code.

La fascia in mezzo è isteresi. Se si raddoppiasse e dimezzasse alla stessa soglia, il
controllore oscillerebbe. I punti misti tra 1 e 5 % (Aggressive a 2048 code a 3.6 Mpps:
`s` = 2.5 % con 1.35 % di perdita) dicono anche che 5 % non garantisce perdita zero sotto
soglia. È un compromesso tra reagire presto e non crescere per picchi innocui.

## 3. Quando decide

Il controllore gira in testa al loop, tra uno sweep e l'altro, quando sono passati
`--adaptive-epoch` ms (default **100 ms**) di TSC. Il tempo del controllore e delle stampe non
viene contato nello sweep successivo.

Con code molto piene uno sweep può durare più di un'epoca. In quel caso la decisione arriva
alla fine dello sweep.

## 4. Le condizioni, in ordine

Parametri e default:

| simbolo | opzione | default |
|---|
| `H` | `--adaptive-backlog-high` | 5 % |
| `L` | `--adaptive-backlog-low` | 0.5 % |
| `E` | `--adaptive-empty-high` | 50 % |
| `T` | `--adaptive-threshold` | 3 % |
| `hold` | `--adaptive-hold` | 20 epoche (2 s) |
| `W` | `ADAPTIVE_FLAP_WINDOW` | 10 epoche (1 s) |
| reset | `ADAPTIVE_FLAP_RESET` | 600 epoche (60 s) |
| min pkt | `ADAPTIVE_MIN_EPOCH_PKTS` | 1000 |

`adaptive_epoch()` valuta questi passi in ordine e si ferma al primo che restituisce un
numero di code.

**Passo 0: contatori.** Decrementa `no_grow` e `no_shrink`. Dopo 600 epoche senza cambi azzera
`flaps`.

**Passo 1: settle.** Se il numero di code è appena cambiato, scarta questa epoca e non decide.
Nell'epoca dopo un cambio le code in drain si svuotano e le cache si scaldano, quindi le misure
non sono rappresentative. Ogni passo costa quindi almeno due epoche, circa 200 ms.

**Passo 2: poco traffico.** Con meno di 1000 pacchetti negli sweep con lavoro non c'è niente da
imparare e il numero di code resta com'è. Senza traffico il controllore non riduce: resta dove
era.

**Passo 3: la crescita è servita?** Si applica solo alla prima epoca misurata dopo un raddoppio.
Il raddoppio viene **annullato** se valgono tutte e tre le condizioni:

```
s > H                         la share è ancora sopra la soglia
s >= share prima del raddoppio    e non è scesa
c > costo prima * (1 - T)     e il costo per pacchetto non è sceso di almeno il 3%
```

In quel caso il controllore:
- dimezza subito;
- blocca la crescita per `no_grow = hold << min(flaps, 3)` epoche (da 2 s fino a 16 s);
- incrementa `flaps`.

Il caso tipico è Baseline: con più code ci sono più ring da visitare, ognuno con 32 pacchetti
pronti per visita, e la share non scende.

**Passo 4: la mossa proposta.**

Si arriva qui solo se i passi 1-3 non hanno già deciso. È quindi un'epoca misurata (non di
settle), con almeno 1000 pacchetti, e non è un veto. Il controllore propone **al più una mossa per
epoca**: raddoppiare, dimezzare o restare. La proposta si decide in due tempi.

*Primo: in che zona è la backlog share `s` dell'epoca?*

```
        s < L (0.5 %)          L <= s <= H            s > H (5 %)
  ─────────────────────┬───────────────────────┬──────────────────────
   meno di 1 burst      zona buona:              più di 1 burst pieno
   pieno su 200 lascia  le code bastano e        su 20 lascia oltre
   oltre metà ring:     non sono troppe          metà ring in coda:
   code troppe                                   rischio di perdita
   → dimezza            → resta, salvo poll      → raddoppia, o dimezza
                          vuoti (sotto)            se raddoppiare è vietato
```

*Secondo: il codice valuta tre regole in cascata (`if / else if / else if`). Vale la prima
condizione vera, e le altre non vengono guardate.*

```c
if      (s > H && cur < max && no_grow == 0)   move = +1;  // regola A
else if (s > H && no_grow > 0 && cur > min)    move = -1;  // regola B
else if ((e > E || s < L) && cur > min)        move = -1;  // regola C
else                                           move =  0;  // resta
```

**Regola A: raddoppia.** Si applica quando i ring sono troppo pieni (`s > H`), si può ancora
crescere (non si è a `-q`) e la crescita non è vietata (`no_grow == 0`). È il caso normale di
sovraccarico, per esempio quando il rate sale.
- *Esempio*: Aggressive, `flows-rate3`, 10000 flussi a 3 Mpps su 1 coda: `s` ≈ 98 %. Il
  controllore raddoppia più volte e a 32 code `s` scende a ~2 %, nella zona buona.

**Regola B: dimezza perché raddoppiare non è servito.** Si applica quando i ring sono ancora
troppo pieni (`s > H`) ma la crescita è vietata (`no_grow > 0`). Il divieto c'è solo se, entro le
ultime `hold << flaps` epoche, il passo 3 ha annullato un raddoppio. Visto che più code non hanno
aiutato, il controllore prova con meno code: con Baseline meno code vuol dire meno ring da
visitare e il backlog si smaltisce prima.
- Senza un veto recente questa regola non scatta mai.
- *Esempio*: Baseline in sovraccarico a 2048 code. Il raddoppio 1024 → 2048 viene annullato,
  e da lì la regola B spinge verso il basso.

**Regola C: dimezza perché le code sono troppe.** Si valuta solo se A e B non si applicano, cioè
in pratica quando `s <= H`. Scatta per una di due ragioni:
- `s < L`: quasi nessun burst pieno lascia il ring oltre metà, quindi il core tiene il passo
  anche con meno code. È il caso del carico che scende. Un'epoca con traffico ma senza nessun
  burst pieno ha `s = 0`, quindi rientra qui;
- `e > E`: negli sweep che hanno trovato lavoro, più della metà dei poll legge zero pacchetti.
  Il core perde tempo a visitare code vuote anche se il backlog è nella zona buona.
- *Esempio*: `load` a 0.5 Mpps all'avvio con 2048 code: `s = 0` e quasi tutti i poll vuoti. La regola C
  dimezza a ogni decisione e in ~3 s porta il controllore a 1 coda.

**Resta** quando nessuna regola si applica:
- `L <= s <= H` e `e <= E`, cioè la zona buona senza troppi poll vuoti: è lo stato di regime;
- oppure si è già al limite nella direzione voluta: `s > H` a `-q` code, oppure si vuole
  dimezzare ma si è già a `--adaptive-min`.

  Un caso limite: con `s > H` a `-q` code e `no_grow == 0` la regola A non si applica (non si
  può crescere) e la B nemmeno (crescere non è vietato). Si passa allora alla regola C, che può
  dimezzare se `e > E`.

Alla proposta di questo passo si applicano poi i filtri del passo 5, che possono ancora
annullarla.

Siccome ogni decisione è seguita da un'epoca di settle, il controllore si muove al massimo di un
fattore 2 ogni due epoche (200 ms). Da 2048 a 1 code ci vogliono 11 dimezzamenti, circa 2.2 s.

**Passo 5: filtri anti-oscillazione** sulla mossa proposta.

| condizione | effetto |
|
| dimezzare entro `W` epoche da un raddoppio | ignorato: lascia assestare le code nuove |
| dimezzare con `no_shrink > 0` | ignorato |
| raddoppiare entro `W` epoche da un dimezzamento | è un **flap**: l'ultimo dimezzamento era di troppo. Il raddoppio avviene, ma blocca le riduzioni per `no_shrink = hold << min(flaps,3)` epoche e incrementa `flaps`. A parità di oscillazione vince il numero più alto |

**Passo 6: applicazione.**
- `cur_log ± 1`, `settle = true`;
- se è un raddoppio, salva `c` e `s` per il controllo del passo 3;
- `adaptive_step` chiama `set_active_queues(1 << cur_log)` e stampa
  `adaptive: X -> Y queues after signal epoch: <c> cycles/pkt (base <c prima>), empty <e>%, backlog share <s>%`.

### Due proprietà che derivano da queste regole

- **Un passo alla volta.** Il controllore vede solo l'effetto di un raddoppio. Se il guadagno
  arriva solo dopo molti raddoppi, con il primo che non migliora niente, il passo 3 annulla la
  crescita e il controllore resta fermo.
- **Blocco reciproco.** Se `no_grow > 0` (dopo un veto) e `no_shrink > 0` (dopo un flap) sono
  attivi insieme e `s > H`, la regola (b) propone di dimezzare ma il filtro `no_shrink` la
  blocca. Il numero di code resta fermo con i ring pieni finché uno dei due blocchi non scade,
  fino a 16 s.

Tutti e due i casi si vedono nei risultati qui sotto.

## 5. Risultati

### 5.1 Throughput a perdita zero (`results/adaptive/ndr_v2/`)

`adaptive_ndr.py` cerca il rate a perdita zero (perdita sotto 0.1%, trial con warmup escluso)
per:
- ogni numero fisso di code (`static-N`);
- il controllore `signal`.

Lo fa con skew 0.6, pacchetti da 64 B e 100-15000 flussi. Valori in Mpps:

| flussi | policy | migliore fisso (code) | 1 coda | 2048 code | **signal** (code finali) | Δ signal |
|---|---|---|---|---|---|---|
| 100 | aggressive | 4.28 (2) | 3.83 | 4.01 | **4.28** (4) | 0.0 % |
| 100 | baseline | 4.24 (1) | 4.24 | 2.30 | **2.74** (1) | **−35.5 %** |
| 100 | penalty | 4.25 (1) | 4.25 | 3.49 | **4.23** (1) | −0.6 % |
| 1000 | aggressive | 4.01 (64) | 3.93 | 3.61 | **3.80** (1) | −5.1 % |
| 1000 | baseline | 3.88 (1) | 3.88 | 1.96 | **3.90** (1) | +0.4 % |
| 1000 | penalty | 3.93 (1) | 3.93 | 3.02 | **3.95** (1) | +0.7 % |
| 5000 | aggressive | 3.71 (128) | 3.02 | 3.32 | **3.64** (128) | −1.8 % |
| 5000 | baseline | 3.09 (1) | 3.09 | 1.64 | **2.94** (1) | −4.9 % |
| 5000 | penalty | 3.02 (1) | 3.02 | 2.65 | **2.93** (128) | −3.2 % |
| 10000 | aggressive | 3.49 (128) | 2.59 | 3.24 | **3.45** (256) | −1.2 % |
| 10000 | baseline | 2.72 (8) | 2.59 | 1.67 | **2.83** (1) | +4.0 % |
| 10000 | penalty | 2.74 (32) | 2.56 | 2.60 | **2.75** (1) | +0.5 % |
| 15000 | aggressive | 3.21 (128) | 2.31 | 3.11 | **3.16** (256) | −1.7 % |
| 15000 | baseline | 2.61 (8) | 2.31 | 1.67 | **2.54** (8) | −2.6 % |
| 15000 | penalty | 2.63 (16) | 2.31 | 2.41 | **2.64** (1) | +0.5 % |

Grafici: `experiments/plots/adaptive/ndr_v2/summary.png` e
`experiments/plots/adaptive/ndr_v2/sweep_{aggressive,baseline,penalty}.png`.

Cosa mostrano i numeri:

- **Il controllore trova la zona giusta per ogni policy.**
  - Con Aggressive il numero di code ottimo cresce con i flussi (2 code a 100 flussi, 128 da
    5000 in su), e signal finisce a 4, 128, 256, 256.
  - Con Baseline e Penalty l'ottimo è 1-32 code e la curva è piatta a sinistra. Signal finisce
    quasi sempre a 1 coda, perché a basso carico la regola (c) lo porta al minimo e lì la share
    resta bassa fino al limite.
- **In 14 casi su 15 signal è entro −5.1 % e +4.0 % dal migliore fisso**. Su quei 14 casi la media è −1.1 %. Valori sopra 0 vogliono dire che signal fa meglio del
  migliore fisso. Una differenza di qualche punto percentuale rientra nella risoluzione della
  ricerca e nella variabilità tra trial: c'è una sola ripetizione.
- **Aggressive a 1000 flussi (−5.1 %)**: signal resta a 1 coda (3.80) mentre l'ottimo è a 64
  (4.01). Ma da 1 a 2 code il guadagno è negativo (3.93 → 3.72), quindi ogni singolo raddoppio
  peggiora. È il limite "un passo alla volta" del §4.
- **Baseline a 100 flussi (−35.5 %) è un artefatto del blocco reciproco**, non della scelta a
  regime. Il log `ndr_v2/logs/f100_baseline_signal_r0.log` mostra cosa succede:
  1. Il primo trial a 4.5 Mpps parte con 2048 code (`--adaptive-start` = `-q`). La share è
     ~99 % e il controllore oscilla 2048 ↔ 1024 quattro volte: ogni raddoppio viene annullato
     dal passo 3 e ogni nuovo raddoppio entro 1 s conta come flap.
  2. Dopo l'ultimo veto restano attivi insieme `no_grow` e `no_shrink`.
  3. Il trial successivo a 2.75 Mpps resta tutto il tempo a 1024 code con share 99 % e perdita
     1.2 %, perché la regola (b) è bloccata da `no_shrink`.
  4. Il trial fallito fissa il limite superiore della ricerca a 2.75. Tutti i trial dopo, a
     1 coda, passano: fino a 2.736 Mpps con perdita ≤ 0.0001 %. Il valore fisso a 1 coda è
     4.24.

  Per i trial dopo il primo la ricerca riusa lo stato del controllore, quindi un trial
  in sovraccarico all'inizio può condizionare quelli successivi.
- **Versione del codice.** `ndr.csv` è delle 02:53 del 14/10, mentre l'ultima modifica a
  `src/adaptive.h` è delle 10:12 dello stesso giorno. Questi numeri vengono quindi da una
  versione precedente del controllore. La timeline del §5.2 (17:53) è stata fatta con il codice
  attuale.

### 5.2 Traffico che cambia nel tempo (`results/adaptive/timeline/`)

`adaptive.py` cambia il traffico ogni 20 s. Ogni scenario gira con signal e con 1, 128, 512 e
2048 code fisse. Throughput ricevuto medio sull'intera corsa e perdita media per fase:

| scenario | policy | signal | 1 | 128 | 512 | 2048 |
|---|---|---|---|---|
| `load` 0.5→4→sat→0.5 Mpps, 10k flussi | aggressive | **2.12 Mpps / 6.8 %** | 1.84 / 12.5 % | 2.06 / 7.8 % | 2.09 / 7.3 % | 2.12 / 6.8 % |
| | baseline | **1.83 / 13.0 %** | 1.81 / 13.3 % | 1.82 / 13.1 % | 1.88 / 12.0 % | 1.95 / 10.8 % |
| | penalty | **1.85 / 12.5 %** | 1.81 / 13.2 % | 1.81 / 13.2 % | 1.90 / 11.2 % | 2.00 / 9.4 % |
| `flows-rate` 2.5 Mpps, 100→15k→100 flussi | aggressive | **2.32 / 5.3 %** | 2.18 / 11.2 % | 2.45 / 0.003 % | 2.45 / 0.003 % | 2.46 / 0.005 % |
| | baseline | **2.22 / 9.2 %** | 2.13 / 13.3 % | 2.44 / 0.2 % | 0.11 / 95.6 % ⚠ | 2.39 / 2.8 % |
| | penalty | **2.24 / 8.8 %** | 2.14 / 12.7 % | 2.44 / 0.003 % | 2.46 / 0.003 % | 2.45 / 0.013 % |
| `flows-rate3` 3 Mpps, 100→15k→100 flussi | aggressive | **2.80 / 4.8 %** | 2.27 / 23.0 % | 2.94 / 0.005 % | 2.94 / 0.006 % | 2.94 / 0.008 % |
| | baseline | **2.30 / 21.8 %** | 2.28 / 22.8 % | 2.81 / 4.4 % | 2.79 / 5.2 % | 2.75 / 6.5 % |
| | penalty | **2.33 / 21.0 %** | 2.45 / 16.9 % | 2.84 / 3.4 % | 2.89 / 1.9 % | 2.93 / 0.6 % |

⚠ `flows-rate` Baseline a 512 code fisse è entrato nello stallo da sovraccarico delle code QDMA
(0.11 Mpps per quasi tutta la corsa). È un problema della NIC, non del controllore.

Grafici per fase: `experiments/plots/adaptive/timeline/phases_<scenario>_<policy>.png`. Istantanee
delle code: `snapshots_<scenario>_<policy>_<modo>.png`.

**`load` (salita e discesa del rate).**
- A 0.5-2 Mpps signal scende a 1 coda in ~3 s dall'avvio, con la sequenza 2048 → 1024 → … → 1
  in `decisions.csv`, e ci resta senza perdite (≤ 0.002 %).
- Con Aggressive, da 3 Mpps sale (media 51 code nella fase a 3 Mpps, poi 2048 nelle fasi in
  sovraccarico) e ottiene lo stesso risultato della configurazione fissa migliore (2048 code,
  2.12 Mpps). Quando il carico torna a 2 Mpps riscende a 1 coda.
- Con Baseline e Penalty resta a 1 coda anche in sovraccarico e ottiene il risultato di 1 coda
  fissa. In queste fasi però 2048 code fisse perdono un po' meno (10.8 % contro 13.0 % di
  Baseline).

**`flows-rate` (2.5 Mpps, flussi che cambiano).**
- Fino a 10000 flussi 1 coda regge e signal resta a 1-2 code con perdita ≤ 0.4 %.
- A 15000 flussi ogni policy resta a 1 coda (Aggressive passa per 8) con il 42-74 % di perdita,
  mentre 128 code fisse non perdono. Questo è il limite "un passo alla volta": il primo
  raddoppio non abbassa né la share né il costo, il passo 3 lo annulla e `no_grow` impedisce di
  riprovare per un po'.

**`flows-rate3` (3 Mpps).**
- Baseline e Penalty da 10000 flussi in su: share al 100 % per 40 s a 1 coda, perdita 79 % e
  93 %, contro il 4-6 % delle code fisse.
  - `decisions.csv` mostra un tentativo 1 → 2 ogni ~16 s (`no_grow` al massimo, `20 << 3`
    epoche), annullato nell'epoca dopo (es. 14696 → 14985 cicli/pkt a 15000 flussi).
  - Con 128 code il guadagno c'è, ma il controllore non lo vede mai, perché a 2 code il costo
    sale.
- Aggressive a 10000 flussi resta a 1 coda per ~8 s con ~4 % di perdita.
  1. Poi il raddoppio passa (share 98 → 58 → 3 %) e in 4 s arriva a 16-32 code, con perdita
     zero da lì in poi.
  2. A 15000 flussi sale fino a 2048, ma perde comunque il 37 %. Riceve 1.9 Mpps con burst
     pieni al 91 % e share al 41 %, mentre 2048 code fisse ricevono 3.0 Mpps senza perdite
     (burst pieni al 22 %, share all'1.2 %). Nell'ultima decisione il costo per pacchetto
     della corsa signal è ~1600 cicli, contro i ~1030 della fase prima.

  La causa della differenza con la corsa a 2048 fisse **non è verificata**. Nel passaggio a
  15000 flussi il controllore era a 64 code, con una fase transitoria a 0.9 Mpps: potrebbe
  essere lo stesso stallo QDMA del caso ⚠, ma non è dimostrato.

### 5.3 In sintesi

- **A regime e con traffico stabile** (NDR) signal sceglie un numero di code entro pochi punti
  percentuali dall'ottimo fisso per tutte e tre le policy. Riduce le code da solo quando il
  carico scende, e così evita i costi di troppe code vuote (Baseline a 2048 code perde fino al
  50 % rispetto a 1 coda).
- **I suoi limiti vengono dalle regole del §4**:
  - fa un passo alla volta, e il veto del passo 3 lo blocca quando il guadagno arriva solo dopo
    molti raddoppi (Baseline e Penalty con molti flussi a 3 Mpps, tutte le policy a 15000 flussi
    a 2.5 Mpps);
  - `no_grow` e `no_shrink` attivi insieme lo fermano con i ring pieni (NDR Baseline a 100
    flussi).

  Due modifiche possibili sono provare un salto di più ottave prima di annullare una crescita
  e non bloccare la regola (b) con `no_shrink`.
