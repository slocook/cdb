// Multithreaded crash: one thread dereferences null
#include <cstdio>
#include <thread>
#include <chrono>

void worker(int id) {
    // Simulate work
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    if (id == 2) {
        int* p = nullptr;
        *p = 42;  // crash here
    }
    // Other threads just wait
    std::this_thread::sleep_for(std::chrono::seconds(5));
}

int main() {
    std::thread t1(worker, 1);
    std::thread t2(worker, 2);
    std::thread t3(worker, 3);
    t1.join();
    t2.join();
    t3.join();
    return 0;
}
