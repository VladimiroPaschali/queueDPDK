import time
from tqdm import tqdm

class CustomNDRPlugin:
    iter_time = 0.0
    def __init__(self):
        pass

    def pre_iteration(self, finding_max_rate, run_results=None, **kwargs):
        """Eseguito prima di ogni iterazione"""
        self.iter_time = time.time()  # Inizia a contare il tempo dell'iterazione
        pass

    def post_iteration(self, finding_max_rate, run_results, **kwargs):
        """Eseguito dopo ogni iterazione - stampa le statistiche"""
        #usa il tempo reale non quello di iterazione per avere un'idea più precisa del tempo totale del benchmark (incluso warmup)
        self.iter_time = time.time() - self.iter_time
        try:
            tx_pps = run_results.get('tx_pps', 0)
            rx_pps = run_results.get('rx_pps', 0)
            drop_rate = run_results.get('drop_rate_percentage', 0)
            # elapsed = run_results.get('Elapsed Time', 0)
            iteration = run_results.get('total_iterations', 'N/A')
            rate_p = run_results.get('rate_p', 0)
            tqdm.write(f"  [Iter {iteration}] TX: {tx_pps/1e6:.2f}Mpps | RX: {rx_pps/1e6:.2f}Mpps | Drop: {drop_rate:.3f}% | Rate%: {rate_p:.1f}% | Time: {self.iter_time:.1f}s")
            # print(f"  [Iter {iteration}] TX: {tx_pps/1e6:.2f}Mpps | RX: {rx_pps/1e6:.2f}Mpps | Drop: {drop_rate:.3f}% | Rate%: {rate_p:.1f}% | Time: {self.iter_time:.1f}s")
        except Exception as e:
            print(f"❌ Plugin error: {e}")
        
        return False  # Continua il benchmark


def register():
    """TRex plugin registration"""
    return CustomNDRPlugin()
