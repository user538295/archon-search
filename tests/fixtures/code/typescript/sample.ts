/**
 * Sample TypeScript file for C3c fixture.
 *
 * Used by tests/test_code_enricher.py to exercise TypeScript scope-table building.
 */

/** Top-level exported function. */
export function topFn(x: number): number {
  return x * 2;
}

/** Class with a method. */
export class MyClass {
  private value: string;

  constructor(value: string) {
    this.value = value;
  }

  myMethod(): string {
    return this.value.toUpperCase();
  }
}

// Exported const arrow function assigned to a name.
// Arrow functions are treated as enclosing-scope (module-level) code in C3c v1.
export const arrowFn = (): void => {
  console.log("arrow");
};
