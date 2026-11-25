const WORKER_STACK_SIZE: usize = 32 * 1024 * 1024;

fn main() -> anyhow::Result<()> {
    tokio::runtime::Builder::new_multi_thread()
        .thread_stack_size(WORKER_STACK_SIZE)
        .enable_all()
        .build()?
        .block_on(coworld_mtg_server::run())
}
