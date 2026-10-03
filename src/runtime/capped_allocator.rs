//! A V8 `ArrayBuffer` allocator with a hard byte budget.
//!
//! V8 charges `ArrayBuffer` / `SharedArrayBuffer` backing stores to the
//! embedder's allocator, not to the JS heap, so `max_heap_size` alone leaves
//! gigabytes of host RAM reachable from guest code. This allocator refuses any
//! request that would push live bytes past `cap`; V8 then throws a catchable
//! `RangeError: Array buffer allocation failed` instead of the host swapping or
//! being OOM-killed.
//!
//! The budget is *live* bytes: `free` returns what `allocate` took, so a guest
//! that drops its buffers can allocate again.

use std::alloc::{alloc, alloc_zeroed, dealloc, Layout};
use std::ffi::c_void;
use std::ptr;
use std::sync::atomic::{AtomicUsize, Ordering};

use deno_core::v8;

// ponytail: 16 covers every typed-array element size V8 hands the allocator; raise if it ever asks for more.
const ALIGN: usize = 16;

struct Budget {
    live: AtomicUsize,
    cap: usize,
}

impl Budget {
    /// Reserve `len` bytes, or `false` if that would exceed the cap.
    fn reserve(&self, len: usize) -> bool {
        self.live
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |live| {
                live.checked_add(len).filter(|total| *total <= self.cap)
            })
            .is_ok()
    }

    fn release(&self, len: usize) {
        // Saturating: a stray double release must not wrap `live` past `cap`
        // and refuse every later allocation.
        let _ = self
            .live
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |live| {
                Some(live.saturating_sub(len))
            });
    }
}

fn layout(len: usize) -> Option<Layout> {
    // V8 never asks for zero bytes through this path, but `Layout` rejects it.
    Layout::from_size_align(len.max(1), ALIGN).ok()
}

fn take(budget: &Budget, len: usize, zeroed: bool) -> *mut c_void {
    let Some(layout) = layout(len) else {
        return ptr::null_mut();
    };
    if !budget.reserve(len) {
        return ptr::null_mut();
    }
    // SAFETY: `layout` has a non-zero size.
    let p = unsafe {
        if zeroed {
            alloc_zeroed(layout)
        } else {
            alloc(layout)
        }
    };
    if p.is_null() {
        budget.release(len);
    }
    p.cast()
}

unsafe extern "C" fn allocate(budget: &Budget, len: usize) -> *mut c_void {
    take(budget, len, true)
}

unsafe extern "C" fn allocate_uninitialized(budget: &Budget, len: usize) -> *mut c_void {
    take(budget, len, false)
}

unsafe extern "C" fn free(budget: &Budget, data: *mut c_void, len: usize) {
    if data.is_null() {
        return;
    }
    if let Some(layout) = layout(len) {
        // SAFETY: V8 only frees pointers this allocator returned, with the same `len`.
        unsafe { dealloc(data.cast(), layout) };
        budget.release(len);
    }
}

unsafe extern "C" fn drop_budget(budget: *const Budget) {
    // SAFETY: `budget` came from `Arc::into_raw` in `new`.
    drop(unsafe { std::sync::Arc::from_raw(budget) });
}

static VTABLE: v8::RustAllocatorVtable<Budget> = v8::RustAllocatorVtable {
    allocate,
    allocate_uninitialized,
    free,
    drop: drop_budget,
};

/// An allocator that refuses to hold more than `cap_bytes` live.
pub fn new(cap_bytes: usize) -> v8::UniqueRef<v8::Allocator> {
    let budget = std::sync::Arc::new(Budget {
        live: AtomicUsize::new(0),
        cap: cap_bytes,
    });
    // SAFETY: the handle is an `Arc<Budget>` raw pointer and `VTABLE` matches `Budget`.
    unsafe { v8::new_rust_allocator(std::sync::Arc::into_raw(budget), &VTABLE) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reserve_respects_cap_and_release_restores_it() {
        let b = Budget {
            live: AtomicUsize::new(0),
            cap: 100,
        };
        assert!(b.reserve(60));
        assert!(!b.reserve(41));
        assert!(b.reserve(40));
        b.release(60);
        assert!(b.reserve(60));
    }

    #[test]
    fn take_refuses_over_cap_and_free_returns_budget() {
        let b = Budget {
            live: AtomicUsize::new(0),
            cap: 4096,
        };
        let first = take(&b, 4096, true);
        assert!(!first.is_null());
        // zeroed, as `allocate` promises
        assert!(
            unsafe { std::slice::from_raw_parts(first.cast::<u8>(), 4096) }
                .iter()
                .all(|byte| *byte == 0)
        );
        assert!(take(&b, 1, true).is_null(), "budget is exhausted");
        unsafe { free(&b, first, 4096) };
        let again = take(&b, 4096, false);
        assert!(!again.is_null(), "freeing returned the budget");
        unsafe { free(&b, again, 4096) };
    }

    #[test]
    fn free_of_null_and_double_release_are_harmless() {
        let b = Budget {
            live: AtomicUsize::new(0),
            cap: 100,
        };
        unsafe { free(&b, ptr::null_mut(), 50) };
        b.release(10);
        assert_eq!(
            b.live.load(Ordering::Acquire),
            0,
            "release saturates at zero"
        );
        assert!(b.reserve(100));
    }

    #[test]
    fn reserve_does_not_overflow() {
        let b = Budget {
            live: AtomicUsize::new(1),
            cap: usize::MAX,
        };
        assert!(!b.reserve(usize::MAX));
    }
}
