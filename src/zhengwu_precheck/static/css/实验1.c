#define __USE_MINGW_ANSI_STDIO 1
#include <stdio.h>
#include <stdlib.h>

/* 从1开始逐个试除；0单独处理。 */
long long brute(long long a, long long b, long long *steps)
{
    long long g = 1, limit = a < b ? a : b;
    *steps = 0;
    if (a == 0 || b == 0) return a + b;
    for (long long d = 1; d <= limit; ++d) {
        ++*steps;
        if (a % d == 0 && b % d == 0) g = d;
    }
    return g;
}

long long euclid(long long a, long long b, long long *steps)
{
    *steps = 0;
    while (b != 0) {
        long long r = a % b;
        a = b; b = r; ++*steps;
    }
    return a;
}

int main(void)
{
    int x, y, id = 0;
    while (scanf("%d%d", &x, &y) == 2) {
        long long a = llabs((long long)x);
        long long b = llabs((long long)y), s1, s2, g1, g2;
        /* 本实验约定gcd(0,0)=0，以便统一处理。 */
        g1 = brute(a, b, &s1); g2 = euclid(a, b, &s2);
        printf("Case %d\n", ++id);
        printf("Brute: %lld  trials=%lld\n", g1, s1);
        printf("Euclid: %lld  rounds=%lld\n", g2, s2);
    }
    return 0;
}
