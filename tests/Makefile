all: crash_null crash_threads

crash_null: crash_null.cpp
	$(CXX) -g -O0 -o $@ $<

crash_threads: crash_threads.cpp
	$(CXX) -g -O0 -std=c++11 -o $@ $< -lpthread

clean:
	rm -rf crash_null crash_threads *.dSYM

.PHONY: all clean
