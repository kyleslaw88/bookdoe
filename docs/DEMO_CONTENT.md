# Demo content: sources and licences

Everything in the public demo (demo.bookdoe.com) is public domain or openly licensed. The files live in the demo's own media tree and in its seed database, so they survive the daily reset.

`bookdoe/tests/test_demo_content.py` reads the seed title list in `bookdoe/tests/fixtures/demo_seed_titles.json` and fails when a title has no row below. When you add a file to the demo seed, add its title to that list and a row here.

| Title | Type | Source URL | Licence |
| --- | --- | --- | --- |
| The Raven and The Philosophy of Composition (Poe) | audiobook (LibriVox) and EPUB, one work with both | https://archive.org/details/the_raven_and_the_philosophy_of_composition_1711_librivox (LibriVox recording, 3 mp3 parts); https://gutenberg.pglaf.org/cache/epub/17192/pg17192-images.epub (Project Gutenberg #17192, https://www.gutenberg.org/ebooks/17192) | Public domain (LibriVox recordings are public domain; Gutenberg text public domain in the USA) |
| The Gift of the Magi (O. Henry) | audiobook (LibriVox) | https://archive.org/details/giftofmagi (LibriVox recording, 1 mp3) | Public domain |
| The Adventures of Sherlock Holmes (series Sherlock Holmes #3) | EPUB and PDF (1892 Grosset & Dunlap scan, 359 pp) | PDF: https://commons.wikimedia.org/wiki/File:The_Adventures_of_Sherlock_Holmes.pdf (Internet Archive item adventuresofsher00doylrich); EPUB: https://www.gutenberg.org/cache/epub/1661/pg1661-images.epub (Project Gutenberg #1661) | PDF: PD-scan, PD-old-70-expired. EPUB: US public domain |
| The Memoirs of Sherlock Holmes (Sherlock Holmes #4) | EPUB | https://standardebooks.org/ebooks/arthur-conan-doyle/the-memoirs-of-sherlock-holmes | US public domain, Standard Ebooks contribution CC0 |
| The Hound of the Baskervilles (Sherlock Holmes #5) | EPUB | https://standardebooks.org/ebooks/arthur-conan-doyle/the-hound-of-the-baskervilles | US public domain, Standard Ebooks contribution CC0 |
| The Wonderful Wizard of Oz (Oz #1) | EPUB | https://standardebooks.org/ebooks/l-frank-baum/the-wonderful-wizard-of-oz | US public domain, Standard Ebooks contribution CC0 |
| The Marvelous Land of Oz (Oz #2) | EPUB | https://standardebooks.org/ebooks/l-frank-baum/the-marvelous-land-of-oz | US public domain, Standard Ebooks contribution CC0 |
| Frankenstein (Mary Shelley) | EPUB | https://www.gutenberg.org/cache/epub/84/pg84-images.epub (Project Gutenberg #84) | Public domain in the USA (Project Gutenberg licence) |
| Pride and Prejudice (Jane Austen) | EPUB | https://www.gutenberg.org/cache/epub/1342/pg1342-images.epub (Project Gutenberg #1342) | Public domain in the USA (Project Gutenberg licence) |
| Alice's Adventures in Wonderland (Lewis Carroll) | EPUB | https://www.gutenberg.org/cache/epub/11/pg11-images.epub (Project Gutenberg #11) | Public domain in the USA (Project Gutenberg licence) |
| Moby Dick (Herman Melville) | EPUB | https://www.gutenberg.org/cache/epub/2701/pg2701-images.epub (Project Gutenberg #2701) | Public domain in the USA (Project Gutenberg licence) |
| LibriVox Short Story Collection 090 | podcast (2 episodes, 2020-06-02 and 2020-06-09) | LibriVox podcast feed for the collection, https://librivox.org | Public domain (LibriVox recordings) |
| LibriVox Short Story Collection Vol 087 | podcast (2 episodes, 2020-05-19 and 2020-05-26) | LibriVox podcast feed for the collection, https://librivox.org | Public domain (LibriVox recordings) |

Coverage: ebook (EPUB), PDF, audiobook, a work with both audiobook and ebook (The Raven), podcasts, and series (Sherlock Holmes, Oz).

Evidence for the 2026-11-25 additions is on <owner>/bookdoe#740 and #741.
