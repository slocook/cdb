// Deliberately crashes: null pointer dereference through a method call
#include <cstdio>

struct Node {
    int value;
    Node* next;

    void print() {
        printf("value=%d\n", value);
        if (next) next->print();  // will crash when next is garbage
    }
};

Node* make_list(int n) {
    if (n == 0) return nullptr;
    Node* node = new Node();
    node->value = n;
    node->next = make_list(n - 1);
    return node;
}

void process(Node* head) {
    // Bug: doesn't check for null before calling method
    Node* bad = nullptr;
    bad->print();  // crash here
}

int main() {
    Node* list = make_list(3);
    process(list);
    return 0;
}
