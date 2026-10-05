//! Allocate wire-compatible u32 handles without replacing live registrations.
use std::collections::HashMap;

pub(crate) fn allocate_id<T>(next: &mut u32, entries: &HashMap<u32, T>) -> Option<u32> {
    // At most entries.len() occupied candidates can precede a free ID.
    // Iterating this bound also detects exhaustion without counter overflow.
    for _ in 0..=entries.len() {
        let id = *next;
        *next = next.wrapping_add(1);
        if !entries.contains_key(&id) {
            return Some(id);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wraps_and_skips_live_ids_without_replacing_them() {
        let mut entries = HashMap::from([(u32::MAX, "last"), (0, "first"), (1, "second")]);
        let mut next = u32::MAX;
        let id = allocate_id(&mut next, &entries).unwrap();
        assert_eq!(id, 2);
        assert_eq!(next, 3);
        assert!(entries.insert(id, "new").is_none());
        assert_eq!(entries[&u32::MAX], "last");
        assert_eq!(entries[&0], "first");
        assert_eq!(entries[&1], "second");
    }

    #[test]
    fn allocates_maximum_id_and_then_wraps() {
        let mut next = u32::MAX;
        let mut entries = HashMap::<u32, ()>::new();
        assert_eq!(allocate_id(&mut next, &entries), Some(u32::MAX));
        entries.insert(u32::MAX, ());
        assert_eq!(allocate_id(&mut next, &entries), Some(0));
    }
}
