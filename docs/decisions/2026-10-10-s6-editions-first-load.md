# S6 Home Office asylum support by local authority (Asy_D11 and Reg_02): first load onto editions

Status: done 2026-10-10. Works like [S22](2026-10-09-s22-editions-first-load.md) (several specs applied together) and
[S23](2026-10-09-s23-editions-first-load.md) (discovery that fails loudly). Loader: `scripts/s6_asylum_editions.py`
(four editions specs in one module); gates: `scripts/s6_asylum_editions_verify.py` (22 gates). Design:
`docs/superpowers/specs/2026-10-10-s6-editions-design.md` (audit record, not approval).

No held value changed. The four live tables are exactly as they were: same rows, same columns, same `loaded_at`
(`la_immigration_groups` 2026-03-31 still carries run 82's, everything else run 98's) and the same survey hashes. The
map is unaffected: S6 is not a W1 input (`refresh_map.py` was not run, nor W1, the export, `push.py` or `git push`).

## Before-state (read-only, confirmed against the live tables before anything was written)

| Table | Rows | Periods | Survey hash (md5) |
|---|---:|---|---|
| `la_asylum_support` | 21,953 | 34 quarters, 2018-03-31 to 2026-06-30 | 5a8a9c162bbbe545427c07a754036166 |
| `la_asylum_support_unallocated` | 84 | 28 quarters, 2018-03-31 to 2024-12-31 | eb0d207bef3a370a7668e04c5718ca82 |
| `asylum_support_non_england` | 2,553 | 34 quarters | 1446442a812148e42465e122353da73e |
| `la_immigration_groups` | 7,104 | 2026-03-31 and 2026-06-30 (3,552 each) | d5b7203bc078c61f5bdb84fa95d2bcf9 |

- One `loaded_at` in the three Asy_D11 tables, 2026-09-04 18:20:20.096642 UTC, `source_edition` `year ending June 2026`.
  `la_immigration_groups` 2026-03-31: `loaded_at` 2026-07-26 19:33:11.029590 UTC, `year ending March 2026`; 2026-06-30:
  2026-09-04 18:20:20.096642 UTC, `year ending June 2026`.
- NULL and 0 counts per column: the three Asy_D11 tables have no NULL and no 0 in `people`, and `source_marker` is
  NULL in every `la_asylum_support` row. `la_immigration_groups`: `people` NULL in 4 rows (Homes for Ukraine, City of
  London and Isles of Scilly, both periods; `suppressed` true, `source_marker` `*`) and 0 in 2,614 (1,304 and 1,310);
  `percentage_of_population` NULL in 6,512 rows and 0 in 2; `source_marker` NULL in 7,096.
- The per-period lines below were computed from the live rows before the migration. Each is `rows_content_sha` (key
  and compared columns, the engine's own content hash) of the period's rows, the 64-hex sha256 written as two 32-hex
  halves so the credential scan does not flag it. Edition 1 "as loaded" hashes to the same lines (verify gate 15
  reads them from this note). The second block, `before-state-all`, hashes every stored column except `loaded_at`
  (including `source_edition`); it is for the record and is not read by a gate. Both were recomputed after the
  migration and are identical (nothing in a live table moved). 34 + 28 + 34 + 2 = 98 lines in each block.

before-state la_asylum_support 2018-03-31 rows=393 sha256-first32=18e9909a680465741ae353de14f1ce98 sha256-last32=0873a40c7d12414709e136fe89c42f6d
before-state la_asylum_support 2018-06-30 rows=390 sha256-first32=7436705919e439b6fea21cbeb964e66e sha256-last32=75191255195f97abc7e71cb96a6b540f
before-state la_asylum_support 2018-09-30 rows=405 sha256-first32=9d653b13336cc754643a6444d017b2f8 sha256-last32=431af41c79fe32bd54c352cad84d14b5
before-state la_asylum_support 2018-12-31 rows=392 sha256-first32=3a47e930f926af82053848d6f333cdbc sha256-last32=6f04aff30ad1b5749e6319b1dec4b9d2
before-state la_asylum_support 2019-03-31 rows=413 sha256-first32=1c223aa77623ea77e0f32945527bd354 sha256-last32=d601ead44884da8f1ad48b40f849985a
before-state la_asylum_support 2019-06-30 rows=414 sha256-first32=c109a90a59b6f5079a646ae0f2131b2a sha256-last32=3ef8b8a94cd6972335bfb0e1cdb3a72e
before-state la_asylum_support 2019-09-30 rows=413 sha256-first32=6abaafbf086e8ca1a1901551f3c30d9d sha256-last32=d006e4bd22a4175eb6b65d97f77a52ab
before-state la_asylum_support 2019-12-31 rows=420 sha256-first32=ad2b73b405066599db59bd19b678fbf5 sha256-last32=86a1c8a0812ecfcdbb4a6543024abbf2
before-state la_asylum_support 2020-03-31 rows=425 sha256-first32=852844852d37180ddc8d33b3214dbab9 sha256-last32=fd3364dc436a1911b99a469d9f9413d7
before-state la_asylum_support 2020-06-30 rows=439 sha256-first32=58ee4fb4651bffec03ae098b311522b3 sha256-last32=a621574527776ffd2f814b3beaa0da9e
before-state la_asylum_support 2020-09-30 rows=457 sha256-first32=d231641bac6353603f3f149445bb9b98 sha256-last32=9814f65f0c758a511bb73d246ff6cd7d
before-state la_asylum_support 2020-12-31 rows=473 sha256-first32=ca10765c9b9e764e2fe7acc04551e341 sha256-last32=1fb7dac22d68a061b87c6d7e1aa54ec8
before-state la_asylum_support 2021-03-31 rows=489 sha256-first32=8781dbc2e40a2fe51e65933ec40ff172 sha256-last32=e4b708d13f302c4d61539b7a6777289e
before-state la_asylum_support 2021-06-30 rows=500 sha256-first32=edd0d890a1b34e16d92714a543a1551e sha256-last32=f62226f23b89eba30dacf2b7b5ca1157
before-state la_asylum_support 2021-09-30 rows=527 sha256-first32=c69d3c74a44d8f47be6f9da7f415aa7d sha256-last32=91bd8f072765fffd88f75d0ac82cdcb4
before-state la_asylum_support 2021-12-31 rows=546 sha256-first32=76519794423a6fe3a07ad0ae542fde1c sha256-last32=ded2fc0472470082086ee11d3719440d
before-state la_asylum_support 2022-03-31 rows=553 sha256-first32=939b6a47f87cf80a03360441f069d1ef sha256-last32=cb3a5690daac25e245d196d89c05f371
before-state la_asylum_support 2022-06-30 rows=567 sha256-first32=603da933617c4e3b9956cc277a554055 sha256-last32=acbf62362a517e7ff80f1a2c210bcaad
before-state la_asylum_support 2022-09-30 rows=575 sha256-first32=38180ab93359675c76cf46b05cafb346 sha256-last32=bc294a206422d3ac5f57cbf82ba0d013
before-state la_asylum_support 2022-12-31 rows=772 sha256-first32=be0bd09ef3a7745e221b8b3c3b5f75d0 sha256-last32=6c96e4331f9cac2b0bfb7c904c79339c
before-state la_asylum_support 2023-03-31 rows=859 sha256-first32=c8bcd8a337ba979c1fcff19f8425fa67 sha256-last32=d39d899a9c24642e51b3903facde549c
before-state la_asylum_support 2023-06-30 rows=979 sha256-first32=a35a1d5729d122e190927f9d681248f7 sha256-last32=9b024df7034bc10dd775f869e719681b
before-state la_asylum_support 2023-09-30 rows=906 sha256-first32=3a1ee25f5bc2e3db265afeb8bee08148 sha256-last32=1cdc5676c26f5ea34697bcc42737864e
before-state la_asylum_support 2023-12-31 rows=630 sha256-first32=2a916a913624bd7ec6ca3bd1defd6f55 sha256-last32=1858347c870ad1c9d06e549429cf8eb3
before-state la_asylum_support 2024-03-31 rows=708 sha256-first32=ecef2769649372447f3e9b0619e7220b sha256-last32=d8b41918dceb9a6749caff226150fa7c
before-state la_asylum_support 2024-06-30 rows=674 sha256-first32=12b6f8ed1bf203e5f741e0e13ec56704 sha256-last32=efd21453c169e299f9d9e558b04abd93
before-state la_asylum_support 2024-09-30 rows=784 sha256-first32=ec2a259583303e3444b07cf1ba7432ad sha256-last32=4cc94f8ee784bf8147cc5afc50881dfb
before-state la_asylum_support 2024-12-31 rows=783 sha256-first32=fd18cdd1726cfab0fca7e078b3cba75a sha256-last32=c72a22ba2babc2dec5dabe1c8e53e509
before-state la_asylum_support 2025-03-31 rows=1015 sha256-first32=9bd20dfd46d943c0e3ad744c62e50251 sha256-last32=b44cac85d0a2f88a3eaf4fc95f2a1b6b
before-state la_asylum_support 2025-06-30 rows=1016 sha256-first32=bb288ea067c3b658e24f699efb3c9c66 sha256-last32=daeb5efef4d8153d0de3e78caefe0e0c
before-state la_asylum_support 2025-09-30 rows=1034 sha256-first32=7dcd34757ecadaa432fadd59448a8852 sha256-last32=8963ffc4ca33ea624f39625babcf4434
before-state la_asylum_support 2025-12-31 rows=1002 sha256-first32=1f583eecca5331dc08ca41734368c87a sha256-last32=fb007b017bf296155403bc295373a6bb
before-state la_asylum_support 2026-03-31 rows=973 sha256-first32=370f47512f4d037714d3dbaf79ed6438 sha256-last32=50c53588e8879efe59edcdfc88f69d07
before-state la_asylum_support 2026-06-30 rows=1027 sha256-first32=b73c1c4ce639fd93f0476728b993cd1d sha256-last32=ecc5c77a2041c5bc65c3e9bbcc16c9f9
before-state la_asylum_support_unallocated 2018-03-31 rows=1 sha256-first32=f01a156490c54c75e678773922681695 sha256-last32=f7ea911b7056da2203ceacab0a083f13
before-state la_asylum_support_unallocated 2018-06-30 rows=4 sha256-first32=b46149898d72efe82612ae63c8bfc3b5 sha256-last32=89b2fe13b19aa952881b3c461b2cf114
before-state la_asylum_support_unallocated 2018-09-30 rows=3 sha256-first32=13515173a9e22bfb8d3acc745dff351a sha256-last32=873cde45a89596ff80a9a8744c5d52ed
before-state la_asylum_support_unallocated 2018-12-31 rows=4 sha256-first32=3ffe8514369f4f2ff1c44b30a14cb374 sha256-last32=e5479e8af07f22fd423c08c716b4e7d6
before-state la_asylum_support_unallocated 2019-03-31 rows=3 sha256-first32=8a258b025758b1d2d09d10206d1a7a4c sha256-last32=93d3346d6e4674b00067f3c916c9c359
before-state la_asylum_support_unallocated 2019-06-30 rows=4 sha256-first32=83ec3f52dbc4de343e8e2b3221634431 sha256-last32=ebdb0db1c9679cc4fd71aa21e1c3e1d7
before-state la_asylum_support_unallocated 2019-09-30 rows=4 sha256-first32=854a92c5f5f0815828258236f0e661fa sha256-last32=8f63c2f4d0cb224a9d15bc1c43307158
before-state la_asylum_support_unallocated 2019-12-31 rows=4 sha256-first32=8fce9e0feff4ba803a7872416fdc011c sha256-last32=8b63af0b6a679929d1c7ef9068fb5499
before-state la_asylum_support_unallocated 2020-03-31 rows=4 sha256-first32=4975f95efe7a6356d3106af34b499ca9 sha256-last32=5af6645cbea8d9f2259db6b2512bc9e8
before-state la_asylum_support_unallocated 2020-06-30 rows=4 sha256-first32=bc47d8ffc6be6529be11ea150ec88af7 sha256-last32=0814e26bf368a7d3c2da3e8bfbe8bc46
before-state la_asylum_support_unallocated 2020-09-30 rows=4 sha256-first32=f813a9be3f4ef52d97e44ef582b5ba90 sha256-last32=c62c650ad7270bf4c24f348c377c2e98
before-state la_asylum_support_unallocated 2020-12-31 rows=4 sha256-first32=96cdf5d9e177264eca6cfa4eadcc1671 sha256-last32=dfd63f896af6190fcccb3b56c53f8118
before-state la_asylum_support_unallocated 2021-03-31 rows=4 sha256-first32=fcec2a7ba8a03695dcb18af046611456 sha256-last32=0e4f33d849b3ff0a7a8c878e14a7381c
before-state la_asylum_support_unallocated 2021-06-30 rows=4 sha256-first32=d48983be2a4f088415383bc5580f0008 sha256-last32=0d7604afe59506e7f081ad7c159e4e20
before-state la_asylum_support_unallocated 2021-09-30 rows=4 sha256-first32=5aac06de3940d2bdf0c1a555632ff93a sha256-last32=58789222ee120f60bcf9b5a9d8f4b37f
before-state la_asylum_support_unallocated 2021-12-31 rows=4 sha256-first32=2be32e3c3edcf79a9970257e7ca81b5f sha256-last32=1f7dc395ad8ccd82a0cffec72f567622
before-state la_asylum_support_unallocated 2022-03-31 rows=4 sha256-first32=41998f86beacd7689278f1240ebb4574 sha256-last32=af7c41dac8e84bbe1bc3e717903bd704
before-state la_asylum_support_unallocated 2022-06-30 rows=4 sha256-first32=727a16058b29e0d2b9dfc16b6d2516d6 sha256-last32=7c25e6d6a8098ea62f52ba8060c3743b
before-state la_asylum_support_unallocated 2022-09-30 rows=4 sha256-first32=03a7a35e0075b9f6ab01afe56829d562 sha256-last32=6c6104386ab645728c502e47e698bcc9
before-state la_asylum_support_unallocated 2022-12-31 rows=3 sha256-first32=6574f3b2a3d8395558a0b2e48af7514d sha256-last32=d41fec47aef5f4387169ad4e1cb40d34
before-state la_asylum_support_unallocated 2023-03-31 rows=3 sha256-first32=9606928001ff262f64bcf5a25ea3c651 sha256-last32=7bf6e4d2264271a7e3f85b5e49c048fa
before-state la_asylum_support_unallocated 2023-06-30 rows=1 sha256-first32=511cd32c4635eb81fd78f86a80c94b20 sha256-last32=b2b2929e31ef1e64d2dd725548d067eb
before-state la_asylum_support_unallocated 2023-09-30 rows=1 sha256-first32=c450bd059fd38995870a4e376228f24e sha256-last32=a94e545b9ff29c90b4292f45714e5451
before-state la_asylum_support_unallocated 2023-12-31 rows=1 sha256-first32=0e9ea44624d05016ecf04907ea8e8760 sha256-last32=5acdba26e0c83766aa040d9bad696945
before-state la_asylum_support_unallocated 2024-03-31 rows=1 sha256-first32=8f27eda3f2b7ed6b6cc6773ff2d406d3 sha256-last32=02c95c58722e2e0d9662ebc31bef39f7
before-state la_asylum_support_unallocated 2024-06-30 rows=1 sha256-first32=4a0953c4a4ca98e379b97da07b854bb4 sha256-last32=e75bb436187296dfccc74c08509fcf21
before-state la_asylum_support_unallocated 2024-09-30 rows=1 sha256-first32=3b7d4e6f30775e4e53c645496455440a sha256-last32=45af21dc8ce47aa57c8fb5bf224bc105
before-state la_asylum_support_unallocated 2024-12-31 rows=1 sha256-first32=4c362d0af051535a0727051675a29b04 sha256-last32=6501d11192e6047c593792a1c9104df2
before-state asylum_support_non_england 2018-03-31 rows=29 sha256-first32=d9911f65152d56bb771f9a0e522e7f5f sha256-last32=8bae127fdaffa61163c5ec22f15351f2
before-state asylum_support_non_england 2018-06-30 rows=25 sha256-first32=7331a3d66ffc5732d58e5d60a3363e1a sha256-last32=c0850998c0e16546b0bb51eaa1987298
before-state asylum_support_non_england 2018-09-30 rows=25 sha256-first32=aa93b5976ccaa89a2c389c287e482eaa sha256-last32=71f86a5d7a3eb3e4cc1827a0474f42d2
before-state asylum_support_non_england 2018-12-31 rows=27 sha256-first32=3be8d9e59f51ed2afeb92433b88a29a9 sha256-last32=8882de038018a7b2c2129e253bf0f65b
before-state asylum_support_non_england 2019-03-31 rows=30 sha256-first32=4c5f8e96df3660f8c5df350433d936e5 sha256-last32=662d7ffd9be2fd1d0ab2ca254eeeae3f
before-state asylum_support_non_england 2019-06-30 rows=27 sha256-first32=6b550619481e599e7036801714cc08c8 sha256-last32=8a6b11ab4a01b2d904f9973cb32beea6
before-state asylum_support_non_england 2019-09-30 rows=27 sha256-first32=b6a21431851dce3f26d5ab9c6c8fb868 sha256-last32=907294f56e4a20843275d8793431ab3f
before-state asylum_support_non_england 2019-12-31 rows=29 sha256-first32=107f13845de298a9c9c0a4b445b7c7e7 sha256-last32=740f1c63fd5bfe311f0a8df63ce3635d
before-state asylum_support_non_england 2020-03-31 rows=34 sha256-first32=b808f1dd51040c7373324140a1c6f765 sha256-last32=28898d372fc4b0362577f5705c2a77ce
before-state asylum_support_non_england 2020-06-30 rows=37 sha256-first32=6a264f2aea522ff52be608aeb26b8bfb sha256-last32=b3559af5bb9647c208878d23309965ed
before-state asylum_support_non_england 2020-09-30 rows=44 sha256-first32=26c962ee6f729658380417ac273e9370 sha256-last32=183d855f7fbbfe8bc81bd4be9b8fbbd3
before-state asylum_support_non_england 2020-12-31 rows=45 sha256-first32=ee8920e10d20d678c8720acb4e52cafd sha256-last32=53e97afb472da8ee30ab72eaad6b1f56
before-state asylum_support_non_england 2021-03-31 rows=43 sha256-first32=824f30181ce2bb93fe80af8152b7a432 sha256-last32=00ef466a08e80b5a811bffe83e42c020
before-state asylum_support_non_england 2021-06-30 rows=41 sha256-first32=71325eb3ab7a1e549296c0b4b7338264 sha256-last32=30f0613fce2f57e3e077ee37743bcdb2
before-state asylum_support_non_england 2021-09-30 rows=47 sha256-first32=d369f753481bb354f11b333a82ef68d9 sha256-last32=e67befe9365b719bb16ba8e0dc6427cd
before-state asylum_support_non_england 2021-12-31 rows=45 sha256-first32=dd966b6d86f8ca7e54186b1a21238e00 sha256-last32=c326efccaf0a640678ec0d8f83e8b67c
before-state asylum_support_non_england 2022-03-31 rows=52 sha256-first32=40aa98ca13f76fad754a67e8fb4e40ac sha256-last32=d10e76c36e8609656435b49d6399c899
before-state asylum_support_non_england 2022-06-30 rows=61 sha256-first32=9211beecaba0985356120849035139fe sha256-last32=a9a8ad678424154dd5e674fe12ef86f9
before-state asylum_support_non_england 2022-09-30 rows=63 sha256-first32=183da88ffc17d4b5e9cfb4cf22f0ba03 sha256-last32=6f9db67c56a56023ce0e36e6c0dafa4f
before-state asylum_support_non_england 2022-12-31 rows=81 sha256-first32=e720571a30a8a7559af3929bcb76c226 sha256-last32=bd38e686fd78b45974917b4c0dad0104
before-state asylum_support_non_england 2023-03-31 rows=87 sha256-first32=435c4962c9dd1d4f5a2b3a68cfb86f59 sha256-last32=1e5144a16664191a527e684dace580cb
before-state asylum_support_non_england 2023-06-30 rows=104 sha256-first32=20bedb34fe97e00c588202c885ba22b1 sha256-last32=b7a2fcb0aa0036165abff2827b400553
before-state asylum_support_non_england 2023-09-30 rows=108 sha256-first32=0bafee90737a4e84d81564bff3643d93 sha256-last32=7e1ee5af9bc0ee1d00111752879f806a
before-state asylum_support_non_england 2023-12-31 rows=85 sha256-first32=03d74e6a822c7e177e7adc2aecdcca99 sha256-last32=37cb3babd12ccb1d3acc237c66a758b0
before-state asylum_support_non_england 2024-03-31 rows=99 sha256-first32=9736b9aa9e4e8cdf80ef1f0e98f3489c sha256-last32=2bf196006252f77641b9398f73f8bd9e
before-state asylum_support_non_england 2024-06-30 rows=91 sha256-first32=87e5aa2d9846e806f86058c614138a00 sha256-last32=53597bdcf84bb97408af02b90a10156b
before-state asylum_support_non_england 2024-09-30 rows=101 sha256-first32=f3eee4a53d0d1c902ca8bab594800960 sha256-last32=cbb8c89870795ee6c77372b20cf2a237
before-state asylum_support_non_england 2024-12-31 rows=102 sha256-first32=1b8ee0aaed215aab8107579e89051ab2 sha256-last32=a599ff97d88f642296fed9564bb353d5
before-state asylum_support_non_england 2025-03-31 rows=147 sha256-first32=cbd2447ab112695e98f03477e88c0a4a sha256-last32=668019d28663bcc91be47dc588a94dfb
before-state asylum_support_non_england 2025-06-30 rows=149 sha256-first32=8bf0aac39d373811b421d028091c06c6 sha256-last32=6b044908c5d16e99b7afc925b0977b29
before-state asylum_support_non_england 2025-09-30 rows=160 sha256-first32=9d443749ecc77c6de167ada1909b8c36 sha256-last32=bc8c4d7471fba79dacbdba812f444e51
before-state asylum_support_non_england 2025-12-31 rows=164 sha256-first32=584614429f6b20ce93b40360019d68d8 sha256-last32=500b2d1b53596636e9e0148a67ec601a
before-state asylum_support_non_england 2026-03-31 rows=165 sha256-first32=1f17ceb96be4c8f649466ae3ea42d1ff sha256-last32=8180cbcedd823600a6d272fca7283480
before-state asylum_support_non_england 2026-06-30 rows=179 sha256-first32=78254208bb0ace8b5302b6c858b4df3a sha256-last32=a00e1f85efad2ac5a9e3b18945dec881
before-state la_immigration_groups 2026-03-31 rows=3552 sha256-first32=2ddaba01bc1dc7db8ac601aec7b94e0c sha256-last32=b8688929a6e4bc63bd1a94f648d69bfd
before-state la_immigration_groups 2026-06-30 rows=3552 sha256-first32=d9c0be8a2fd458478ea84100334251af sha256-last32=06a0e021de2fc60540f452451afd4e43

before-state-all la_asylum_support 2018-03-31 rows=393 sha256-first32=c5657036ef4005cdc8de642d6c9f6104 sha256-last32=7fddad1966f0e7c8e1e17169a1853fb5
before-state-all la_asylum_support 2018-06-30 rows=390 sha256-first32=eebf945bab8d48ac848ad5e9ce5c3ec0 sha256-last32=452af901eb72b3c9cc0335cd4a60431d
before-state-all la_asylum_support 2018-09-30 rows=405 sha256-first32=61b47ed7e8a32387750c11b983220220 sha256-last32=77b0a8e286bb97d7e4b35e04d637035e
before-state-all la_asylum_support 2018-12-31 rows=392 sha256-first32=df4e9a170a04c2de245f252fb6ed3157 sha256-last32=7cb18738c4d944fba3d018918026df96
before-state-all la_asylum_support 2019-03-31 rows=413 sha256-first32=241625ff301bf723c6f13e7105188451 sha256-last32=af4ac9bb75f6973f7faeccfd0df0d7e8
before-state-all la_asylum_support 2019-06-30 rows=414 sha256-first32=0cc6faf50b165647beee0d4d557f6174 sha256-last32=52ea9e0e133647ba7a360c010d33f5c8
before-state-all la_asylum_support 2019-09-30 rows=413 sha256-first32=ec7d7c32f795ed51d612d56cdec4d07e sha256-last32=f9b3cec9c06eb1bd2f1572d736225152
before-state-all la_asylum_support 2019-12-31 rows=420 sha256-first32=77b6748521141290a01d435cc42c078c sha256-last32=3f6055b06ce6b73f8c61f11098ede5cf
before-state-all la_asylum_support 2020-03-31 rows=425 sha256-first32=87d92441f4329c8697af1cf5cffa22fa sha256-last32=881f16507123573e283f94f7a22b85a9
before-state-all la_asylum_support 2020-06-30 rows=439 sha256-first32=e70b03d9449c03605a2c9f7cac7dcd69 sha256-last32=ad6b70f1f9cf7a78c12408db6819c733
before-state-all la_asylum_support 2020-09-30 rows=457 sha256-first32=2cce3981446183bc55251aae62dcee76 sha256-last32=5a93870f1c571732c7c44dcb9b3f3e7d
before-state-all la_asylum_support 2020-12-31 rows=473 sha256-first32=4a8a8da2787cb42a90c282bb176e4863 sha256-last32=3c8ff778c50ad45425d3d20bd60c7881
before-state-all la_asylum_support 2021-03-31 rows=489 sha256-first32=eae798960444ab95fd14865e157f8962 sha256-last32=39a271328ff225840affe03a6ac172b4
before-state-all la_asylum_support 2021-06-30 rows=500 sha256-first32=5a93c2aaf32e43d97b1522d4996f6be4 sha256-last32=0471ce9ca5147606c359180071c79227
before-state-all la_asylum_support 2021-09-30 rows=527 sha256-first32=c8e0ce249bffb3de0d4261079a83e353 sha256-last32=32865f968e7f6f1874e5f08478bd679c
before-state-all la_asylum_support 2021-12-31 rows=546 sha256-first32=d9c911d8ffe6116a55b4056140d13f60 sha256-last32=0520111847281c4288ea0d5b1ed2bcf0
before-state-all la_asylum_support 2022-03-31 rows=553 sha256-first32=2c310e9eee4d2ff2de322f8e297bb989 sha256-last32=ad17b8ebec09f54ede8477d81e89ecae
before-state-all la_asylum_support 2022-06-30 rows=567 sha256-first32=f28805eea6ccb60cc5d3ea776b738f0d sha256-last32=34476c9238f6c26b481c731d12c8c450
before-state-all la_asylum_support 2022-09-30 rows=575 sha256-first32=68baabb00edc3f477d5e9939cfd05aee sha256-last32=c11c6d8b0b0ede8672cf4bc126f37c33
before-state-all la_asylum_support 2022-12-31 rows=772 sha256-first32=50d49e8263c12fad8cbe35065da049da sha256-last32=8b039c49f258f76771a3aa05eb6f4d20
before-state-all la_asylum_support 2023-03-31 rows=859 sha256-first32=5221c612c81e4054c03d386bdc37d18a sha256-last32=932d5b4eb2d8aeee4ea16205dfb71ee3
before-state-all la_asylum_support 2023-06-30 rows=979 sha256-first32=94ff01aad448c261a2a74cdfe53a997f sha256-last32=befd05e0d95d3c4d97d54eec8c211585
before-state-all la_asylum_support 2023-09-30 rows=906 sha256-first32=d0d59edcad6f195f9626bdaa569171f7 sha256-last32=2074fc51f644e4ba9d5a4ee510e8bd13
before-state-all la_asylum_support 2023-12-31 rows=630 sha256-first32=5a3a8625340120db8b2e0bc818d0e2c0 sha256-last32=92c5dbb1649ab28a83aa6569a6849259
before-state-all la_asylum_support 2024-03-31 rows=708 sha256-first32=5d5dac8f4dc2abd33312efb856102778 sha256-last32=68810df3b8d432683131de9629ad7c62
before-state-all la_asylum_support 2024-06-30 rows=674 sha256-first32=defb36f0816513eca70b0571e67c3b7a sha256-last32=6a9dd71d3100be20d46037850090e0f5
before-state-all la_asylum_support 2024-09-30 rows=784 sha256-first32=3c8180389747eb3e9cfc6cd2b814ab9c sha256-last32=736218b2e93cc03f1e520c73b433be18
before-state-all la_asylum_support 2024-12-31 rows=783 sha256-first32=e7b08b225f5647b3ad69117b810385b7 sha256-last32=6bb91815dea17a23f388227813a931ef
before-state-all la_asylum_support 2025-03-31 rows=1015 sha256-first32=fb648bea8e1e8d6d7fc79548ba635642 sha256-last32=f11ae7cd68e334cd15ffb90630b986f6
before-state-all la_asylum_support 2025-06-30 rows=1016 sha256-first32=de781d492e38009ee6319141fdda32b0 sha256-last32=0690fb68f575e40de1d041379c4a8102
before-state-all la_asylum_support 2025-09-30 rows=1034 sha256-first32=77c7bf3c32d7566baca4d53d1872839c sha256-last32=890e9161489e2484abcaaec7dc6245cd
before-state-all la_asylum_support 2025-12-31 rows=1002 sha256-first32=b06743ce3abd0a3181af2aa50b82cc3a sha256-last32=f7e545f9edb8d774482081c5d5363497
before-state-all la_asylum_support 2026-03-31 rows=973 sha256-first32=683ffa97a366eb69d295ec075f7d6559 sha256-last32=43463a6270d1432cf6ca60fe630c645c
before-state-all la_asylum_support 2026-06-30 rows=1027 sha256-first32=2d1089a9826a5807ecf5083de180ed0b sha256-last32=adf350e4e764eada65403b3a63e8218e
before-state-all la_asylum_support_unallocated 2018-03-31 rows=1 sha256-first32=4f71030b434a1bf3c40ee81757c91ce5 sha256-last32=6cd1ffdf0ced22dc2f0d9274a64c08eb
before-state-all la_asylum_support_unallocated 2018-06-30 rows=4 sha256-first32=6b270ea6f10dc0f0a08f41ba721ac441 sha256-last32=0e250162dd4d8db350ffce703224752c
before-state-all la_asylum_support_unallocated 2018-09-30 rows=3 sha256-first32=58eaf39dbb8dbeeb878273ded98466b0 sha256-last32=dad0c3bf30a94bc8fd60ad1f2b14630c
before-state-all la_asylum_support_unallocated 2018-12-31 rows=4 sha256-first32=4b6f83bdd5c4cb591cba221a0a8af4ba sha256-last32=6a6ee5d800b3b411f27efc9a31150fd3
before-state-all la_asylum_support_unallocated 2019-03-31 rows=3 sha256-first32=15f940029361cbe1cbd59e504b80e7ba sha256-last32=c18c9e3d09cfcafdf758bad86952176c
before-state-all la_asylum_support_unallocated 2019-06-30 rows=4 sha256-first32=f6b46004405ce182753b3c4312b24ca3 sha256-last32=6f52e7526117494d967f3b32324be724
before-state-all la_asylum_support_unallocated 2019-09-30 rows=4 sha256-first32=d0f6a61483d5944bd93fd0b03f220f6f sha256-last32=788a61a90fc3bc6a9a0bbbb2e7e45cc9
before-state-all la_asylum_support_unallocated 2019-12-31 rows=4 sha256-first32=a08a28c5a171c1cdf5f044bc19fb187e sha256-last32=d6e2916cfb96c87cee5789932bbf00b8
before-state-all la_asylum_support_unallocated 2020-03-31 rows=4 sha256-first32=2b915b046c0972d87cc08c8bedb14772 sha256-last32=6d25deb8f5c126dc7c276b7052404581
before-state-all la_asylum_support_unallocated 2020-06-30 rows=4 sha256-first32=e4a71784f3a16c5584b77274e2b44ed3 sha256-last32=2d34b5f7e38bde5cd3286d219bf43b95
before-state-all la_asylum_support_unallocated 2020-09-30 rows=4 sha256-first32=d2a067a1e8c557dc586366a4688776c3 sha256-last32=0362a93bcae5bb419e7d17ffa0befa4e
before-state-all la_asylum_support_unallocated 2020-12-31 rows=4 sha256-first32=592019f0a5499d508635ea5dafc8339f sha256-last32=fc70b716f9c3c052fc576971ca8d5590
before-state-all la_asylum_support_unallocated 2021-03-31 rows=4 sha256-first32=5f0ce531d47d004be843493ef29631eb sha256-last32=86532455faa8457c3d6f59f3973beed5
before-state-all la_asylum_support_unallocated 2021-06-30 rows=4 sha256-first32=5b73a4460c3db8002b136e8370a8a321 sha256-last32=8be216677d99f3b4e1fdb0b34885a034
before-state-all la_asylum_support_unallocated 2021-09-30 rows=4 sha256-first32=ab4286d99ba900447d9de60093b4bc37 sha256-last32=42afad7313245e9c7059adcac12aafdf
before-state-all la_asylum_support_unallocated 2021-12-31 rows=4 sha256-first32=76e78196ece2d9859f10d25c12941a12 sha256-last32=25fa234e2f684ef6c21f4d610d61000f
before-state-all la_asylum_support_unallocated 2022-03-31 rows=4 sha256-first32=daffe596cf02c5d96e765287e4fec2e4 sha256-last32=8f78c76f882e2be93b075ce235776c9a
before-state-all la_asylum_support_unallocated 2022-06-30 rows=4 sha256-first32=9c697984b0ae8710a816ea345b7686ba sha256-last32=944a030ec8298ac689619a4a61e841a9
before-state-all la_asylum_support_unallocated 2022-09-30 rows=4 sha256-first32=0d2d0ffdea0cd841472afbbe582e0147 sha256-last32=e65b84311b8d5e052d4270a75a3b15d5
before-state-all la_asylum_support_unallocated 2022-12-31 rows=3 sha256-first32=c4aab3b9c356f3d73adc6ec3d9a1cb9d sha256-last32=97ef2a7a48b23a8f2ba0b7c6ddf35865
before-state-all la_asylum_support_unallocated 2023-03-31 rows=3 sha256-first32=a37f8872845f7a58e549390448b522d0 sha256-last32=4a06621bc23330e24d302f11c473beb6
before-state-all la_asylum_support_unallocated 2023-06-30 rows=1 sha256-first32=7594b10d11d35b5b1fa78855faa6e683 sha256-last32=c0f2d2a79c0ac145bda0ec8fd07fd78c
before-state-all la_asylum_support_unallocated 2023-09-30 rows=1 sha256-first32=2c731a03c180bedce4932911719cb791 sha256-last32=29894d417f8b1471cf4cfcceafed210f
before-state-all la_asylum_support_unallocated 2023-12-31 rows=1 sha256-first32=3ff9a58eeaefa2f7084ad6e18ad9af09 sha256-last32=ed5cc0f765fbe8062b24ac805df73718
before-state-all la_asylum_support_unallocated 2024-03-31 rows=1 sha256-first32=58152bdca7218129003fea181d74b7f3 sha256-last32=6daa89ab19a50f587f4806da65e7bd81
before-state-all la_asylum_support_unallocated 2024-06-30 rows=1 sha256-first32=ace9fc6477a3d963614ccf63d767c00f sha256-last32=7fbfd6b5febd02e9f674e94ed34493e7
before-state-all la_asylum_support_unallocated 2024-09-30 rows=1 sha256-first32=c7bb946136bf80fa0aa28bff6f277699 sha256-last32=593f6ff283e1a579486d9f83ef1a762b
before-state-all la_asylum_support_unallocated 2024-12-31 rows=1 sha256-first32=f92b8201913832cc0622df17f9e347df sha256-last32=22b74acbe5c2a1def7c6e31d517b1640
before-state-all asylum_support_non_england 2018-03-31 rows=29 sha256-first32=21ed159404cbe6231539160c146b9529 sha256-last32=82965087e4a736c84cc23fd7a0915273
before-state-all asylum_support_non_england 2018-06-30 rows=25 sha256-first32=e550c320272fa1b55d4f745cde68fae4 sha256-last32=186b284716976fff52209cd27000470d
before-state-all asylum_support_non_england 2018-09-30 rows=25 sha256-first32=15df492ef94b9780aac51fbafc73da42 sha256-last32=d1f39acde8d3c3c7ba2a486ef798df14
before-state-all asylum_support_non_england 2018-12-31 rows=27 sha256-first32=def453c757d71f9a6ba95770ad587a28 sha256-last32=c6c5f35a5d0b549661d39daa3c687d42
before-state-all asylum_support_non_england 2019-03-31 rows=30 sha256-first32=b354c34bd808d9d5c8c08a2b04c12402 sha256-last32=dca1c4eaa40672166125d80f4943be49
before-state-all asylum_support_non_england 2019-06-30 rows=27 sha256-first32=85549091f9eff0ddf24fcb7a3d91782e sha256-last32=9779aa55d987e1f1b474cca4bde5a0ba
before-state-all asylum_support_non_england 2019-09-30 rows=27 sha256-first32=0cb32af44b2ba6c5130246c95aa7fc02 sha256-last32=be8c2e7beb7e516bf5357b9d6eaeba27
before-state-all asylum_support_non_england 2019-12-31 rows=29 sha256-first32=32969a7137e4a9e28f3b4939cc6584a2 sha256-last32=b94450f700fcbf97fe0c63d4ff847bd5
before-state-all asylum_support_non_england 2020-03-31 rows=34 sha256-first32=f14c2bdad71dc7289f01410b9ed5eeb9 sha256-last32=8153082cf18dbe8a26349058100a4490
before-state-all asylum_support_non_england 2020-06-30 rows=37 sha256-first32=6fa7d4480b3b57f3cf98d86f304edf0b sha256-last32=1aa53a3ba0e09d9516554c4854322a25
before-state-all asylum_support_non_england 2020-09-30 rows=44 sha256-first32=ab084916919dabe93b92b5531ae0a5c9 sha256-last32=971d0350480f36ecdceb6d8e079b9553
before-state-all asylum_support_non_england 2020-12-31 rows=45 sha256-first32=059686411c9a0e3e024643edb65adc51 sha256-last32=ba111e1decc6d5bbcdb7cbeacae745c5
before-state-all asylum_support_non_england 2021-03-31 rows=43 sha256-first32=97666519c4ffa1609c53f33e11d0d140 sha256-last32=cc528fbe99aff7099ba4875610884a50
before-state-all asylum_support_non_england 2021-06-30 rows=41 sha256-first32=c217c53d80e3d2717b3f0a435088f67f sha256-last32=bc5ce4689f5100912ae92bce09d678ad
before-state-all asylum_support_non_england 2021-09-30 rows=47 sha256-first32=0f5b26fdc7d53adda1620bb5219d410f sha256-last32=3434c3be617695ac437c9483257c132b
before-state-all asylum_support_non_england 2021-12-31 rows=45 sha256-first32=4ec4ad080973113779eb7487fb4a3b3a sha256-last32=7c8c89a1a6b85be2723aaf8ff3f42d62
before-state-all asylum_support_non_england 2022-03-31 rows=52 sha256-first32=fa83f219f267dd46383c12874fe1a04f sha256-last32=69d2b620ff8328f48c9bca14715e869e
before-state-all asylum_support_non_england 2022-06-30 rows=61 sha256-first32=a785b7b0ff58ce0daa261e6d3feb1711 sha256-last32=c3c880c3674122fb60c961e197773315
before-state-all asylum_support_non_england 2022-09-30 rows=63 sha256-first32=8de9a2e91bfb055821877e6a28ac6832 sha256-last32=0eec5bc71f407fbb68f4ec462c9b1301
before-state-all asylum_support_non_england 2022-12-31 rows=81 sha256-first32=87df79432740f0ef7597d2dd33a69cca sha256-last32=bc5e247b352fd5a78db70891526877a9
before-state-all asylum_support_non_england 2023-03-31 rows=87 sha256-first32=1e35ab978e6a8563f44fcb9ff7d0a011 sha256-last32=0ff2470977d01220e84f7addf25c8fbe
before-state-all asylum_support_non_england 2023-06-30 rows=104 sha256-first32=53e51d51b6b18d914aa36bb7edc75d42 sha256-last32=8c4f6f3e8556cead829681d4689ab366
before-state-all asylum_support_non_england 2023-09-30 rows=108 sha256-first32=8e7c6e715d6e1763e4c60889a83f5db6 sha256-last32=243b84240df2d65bd45843f0659c193a
before-state-all asylum_support_non_england 2023-12-31 rows=85 sha256-first32=74107cbc5a05ac4aa683d410acc9caee sha256-last32=5aebbcea5f281cfba2dbebd74591342d
before-state-all asylum_support_non_england 2024-03-31 rows=99 sha256-first32=dd55d3435e8ece853d31a65bdad18726 sha256-last32=0a5c8504cece0d080d638576a5fc3f56
before-state-all asylum_support_non_england 2024-06-30 rows=91 sha256-first32=a740ce77a77b8dd26664246344330bda sha256-last32=dc6736c969180bcd84be3da08e02fce1
before-state-all asylum_support_non_england 2024-09-30 rows=101 sha256-first32=abb737f7e8f8e9edf74fab65afa4271f sha256-last32=f61428fbf3a307581cc9a0662d5d825f
before-state-all asylum_support_non_england 2024-12-31 rows=102 sha256-first32=e2645071d3bc8d0f796e754e3c265a04 sha256-last32=edec4f55f1072ffa89fcf5be13edef60
before-state-all asylum_support_non_england 2025-03-31 rows=147 sha256-first32=8f7e1a04eae6eec58bb624cacd9b0bf6 sha256-last32=39949d6293d1dcdcb727e55218ba7a56
before-state-all asylum_support_non_england 2025-06-30 rows=149 sha256-first32=bd7a379344e7752ab93f73cc84ef685c sha256-last32=03619a62fbb4373dcae53908d45c282c
before-state-all asylum_support_non_england 2025-09-30 rows=160 sha256-first32=2adccfb42abcf63173388b88e57bb9fb sha256-last32=aebf89cbc3f3627fb2a9a69fa062d2eb
before-state-all asylum_support_non_england 2025-12-31 rows=164 sha256-first32=4140c66593768f9761171ec11b9744a3 sha256-last32=2a71ec5d50aeea6ba55e141fec92ecea
before-state-all asylum_support_non_england 2026-03-31 rows=165 sha256-first32=5ec3f40b635b39cd22409f1c461ea872 sha256-last32=88d9bf72b866b7b3283e3a87e1a6fb22
before-state-all asylum_support_non_england 2026-06-30 rows=179 sha256-first32=6e7f41f8c034d4993755873d82b3c703 sha256-last32=56d55c4c2e7fff563ed2591b82cbff20
before-state-all la_immigration_groups 2026-03-31 rows=3552 sha256-first32=ccd215cbf4fe3364a6c1e15ba3cbb93c sha256-last32=adac112bc454ea8a5e43222bc9e97bba
before-state-all la_immigration_groups 2026-06-30 rows=3552 sha256-first32=12ce5b8261ceafdd50e3c4898eb0e641 sha256-last32=07e786e3930216f4a8f864accb0b8067

## What the old build did

`scripts/s6_asylum_build.py` and `scripts/s6_asylum_verify.py` were added in commit 429ce9d (2026-07-25 20:21:19
+0100, 19:21:19 UTC), edited in a975f50 (2026-07-26 20:34:55 +0100: the local code recodes for E07000027, 28 and 189
removed after the `la_code_lookup` fix), a8244e1 (2026-08-20, moved to `scripts/`) and 33defee (2026-09-04 19:31:11
+0100 = 18:31:11 UTC: the Reg_02 period taken from the edition label instead of a fixed anchor).

- It was written to overwrite. Every run discovered the files from the landing pages' link text, downloaded them to
  fixed temp names (overwritten each run, none kept), ran the DDL and `CREATE OR REPLACE VIEW` every time, and upserted
  **every period of the file** into all four tables (`INSERT ... ON CONFLICT DO UPDATE ... loaded_at = now()`). It
  deleted and re-inserted `asylum_series_breaks` every run, took the edition label from the link text rather than the
  file, had no preview and no `--commit` (it wrote when run), and its verify module rewrote
  `docs/s6_source_anomalies.md` on every run, even one that was then rolled back.
- **It ran 15 times that left a trace.** `pipeline_run_log` ids 69 to 82 (14 runs, 2026-07-25 18:08 UTC to 2026-07-26
  19:33 UTC, 26,936 rows each, `year ending March 2026`) and id 98 (2026-09-04 18:20:16 to 18:20:20 UTC, 28,142 rows,
  `year ending June 2026`), all `success`. Seven of the runs (69 to 75) came before the code was first committed
  (19:21 UTC on 25 July). Run 98 came 11 minutes before 33defee. The decision note
  [2026-09-04-s6-reg02-hardcoded-snapshot-period.md](2026-09-04-s6-reg02-hardcoded-snapshot-period.md) records two
  failed, rolled-back attempts on 4 September before run 98; the code wrote no run-log row on failure.
- **Evidence in the data.** Every row of the three Asy_D11 tables carries run 98's `loaded_at` (2026-09-04 18:20:20.096642
  UTC) and `year ending June 2026`: run 98 rewrote the 33 periods first loaded by runs 69 to 82 and added 2026-06-30
  (21,953 - 20,926 = 1,027 rows, exactly 2026-06-30's count). `la_immigration_groups` 2026-03-31 still carries run
  82's `loaded_at`, because the 4 September run took the June snapshot instead and the March file was not read again.

## Why no value changed (was anything lost by the overwrite?)

No value, on the evidence. The Asy_D11 file is a time series: every release restates all 50 quarters. The March 2026
file (recovered from the Wayback Machine, capture 2026-08-13 of the original asset URL; evidence only, not loaded) and
the June 2026 file agree on **every cell of every one of the 49 periods they share**, and the December 2025 file
agrees with the March 2026 file on all 48 shared periods. Built through the new parser, March 2026 equals June 2026
on all 99 shared table-periods and December 2025 equals June 2026 on all 96. So the 4 September run rewrote the 2018
to March 2026 rows with values identical to those it replaced; only `loaded_at` and `source_edition` were rewritten.
The overwritten Reg_02 snapshot the September note describes never committed. The proof run below goes further and
reproduces every held row from the files.

## The proof run

`migrate-legacy` with the held June 2026 Asy_D11 file, the March and June 2026 Reg_02 files and the June 2026 Asy_D09
file (sha256 prefixes d2207eed53afa18c, 1dd0e75630e1531a, 5d857c6d55bbde81 and 72f915e4ae27e78b), in one
transaction. The new parser on the held files reproduces every live row and every stored column:

- `la_asylum_support` 21,953 rows / 65,859 cells, `la_asylum_support_unallocated` 84 / 84,
  `asylum_support_non_england` 2,553 / 7,659, `la_immigration_groups` 7,104 / 42,624: **0 differences**
  (`published_la_name`, `source_marker`, `country`, `suppressed`, `population` and `percentage_of_population`
  included). Each period's live `source_edition` equals its proved file's own "year ending".
- Reconciled from the files: the data sheet equals the pivot sheet's cache for the latest eight quarters (Grand Total
  97,519 at 2026-03-31 and 93,293 at 2026-06-30); England + non-England + unallocated equal the data sheet total in
  every period; England and unallocated equal Asy_D09 in every period; Reg_02 March and June supported-asylum totals
  equal Asy_D11 for each of the 296 authorities.
- 34 reorganisation merges (predecessor districts summed onto one unitary) and one same-code duplicate, listed:
  2023-03-31 Wolverhampton E08000031 Section 98 Dispersal, published twice under North West (4) and West Midlands
  (12), summed to 16 as the old build did.
- **The merged-authority name rule.** Where several rows reach one key, `published_la_name` (and `source_marker` and
  `country`) is taken from the row with the **highest publisher code** (ties: the greatest name, then marker), not
  from file order. Of the rules tried against the held names, only this one reproduced every held name: "last row in
  file order" reproduced them on the June and March files but gave a different name on four merged keys (E06000066,
  2022-03-31, 2022-06-30, 2022-12-31 and 2023-03-31: "South Somerset" instead of the held "Somerset West and
  Taunton") on the December 2025 file, which orders its rows differently. With the code rule, December 2025 equals
  June 2026 on all 96 shared table-periods. A future release that reorders rows cannot now fake a revision.

## Revision evidence (rule 2)

| Compared | What changed |
|---|---|
| Asy_D11 Dec 2025 to Mar 2026 to Jun 2026 files | no cell in any shared period (48, then 49); each release added one quarter |
| Held data against the June 2026 files | reproduced exactly (the proof run) |
| Asy_D11 13 June 2024 | "second edition": accommodation types revised (initial accommodation down, hotels up) and corrections to the stated geographical distribution; totals unchanged; also Asy_D09 and Reg_02 |
| Reg_02 22 August 2024 | earlier files revised (support and accommodation type, geography) |
| Reg_02 16 December 2024 | Reg_01/Reg_02 Afghan figures revised (232 Northern Ireland cases) |
| Reg_02 27 November 2025 | March 2024 and September 2024 files reissued ("minor revision ... accommodation types"; totals unaffected); covers not updated |

So Asy_D11 normally adds a quarter and leaves the rest alone, but has restated back periods and can again; Reg_02
snapshots are reissued as files for their own period. `revises_back_series` true stands. The loader stores a revised
period as the next edition (per period, under thresholds listed in the source doc; above them REJECTED until named
with `--acknowledge`), and the live table changes only through `refresh-latest`.

## Barnsley and Sheffield: `mixed`

`DATASET_FORM['6']` was `unverified`; it is now `mixed` (one form per table per period), with the evidence read from
the files on 2026-10-10:

- Asy_D11: Barnsley and Sheffield are E08000016 and E08000019 for 31 Mar 2014 to 30 Sep 2025 (47 quarters; 31 of
  them loaded, 2018-03-31 to 2025-09-30) and E08000038 and E08000039 for 31 Dec 2025, 31 Mar 2026 and 30 Jun 2026
  (3 loaded). Never both forms in one period. The switch is by publication, not by the 1 April 2025 boundary date
  (the June and September 2025 quarters still use the old codes).
- Reg_02: March 2026 uses E08000016/19, June 2026 uses E08000038/39.
- The tables hold the old codes throughout (`lad24cd` in `la_boundaries`; E08000038/39 are not in it), resolved
  through `geography.resolve('6', ...)` per table and period, as held. The other predecessor codes (Cumbria,
  North Yorkshire and Somerset 2023: 14 district codes seen up to 2023-03-31) resolve forward through
  `la_code_lookup` rows of type `new_unitary` with a single target in `la_boundaries`; anything else is UNEXPLAINED
  and stops.

## Discovery: the `.xlsx` Reg_02 bug and the URL redirect

- The old build accepted only `.ods` Reg_02 links and took the newest *parseable .ods*. The year ending March 2025
  Reg_02 was published as `.xlsx` (still listed; header "Population " with a trailing space). Had the build run
  between 22 May and 21 August 2025 it would have loaded the December 2024 snapshot as "newest" without a word. It
  did not run then (runs began 25 July 2026), so nothing wrong was stored; the defect was latent. The new loader
  reads the page through the GOV.UK content API, accepts `.ods` or `.xlsx`, takes one release per year-ending, halts
  listing the titles seen if a title changes or two files claim a quarter, and prints the newest release it saw for
  each publication on every run.
- Old asset URLs now **301-redirect to the newest file**: the March 2026 Asy_D11 URL serves the June 2026 file.
  A URL is therefore not evidence of a release. The loader follows redirects, records the final URL in the ledger and
  reads the identity from the file's own cover sheet, `Contents`, data header and (Reg_02) title, never the URL or
  the name.
- The Asy_D11 page lists only the newest file, so a missed quarter cannot be loaded from the page later; `--file`
  with an archived copy is the route (identity from the file).

## Reg_02 reissues and `--accept-reissue`

The publisher's November 2025 reissues of the Reg_02 March 2024 and September 2024 files (new media ids uploaded 19
and 24 November 2025) still say "Published: 22 August 2024" and "Published: 16 December 2024" on their covers.
Rank is (year-ending date, published date) read from the file's cover, so a reissue has **equal rank with different
content**, which is a real case here, not two files claiming one version. It stops unless the period is named with
`--accept-reissue PERIOD`, after reading the page's change note; the next edition is then stored and the choice
logged. Those two files are evidence only; the earlier Reg_02 snapshots are not loaded (follow-up below).

## What was written (2026-10-10)

- `ddl --commit`: the four editions tables (`la_asylum_support_editions`, `la_asylum_support_unallocated_editions`,
  `asylum_support_non_england_editions`, `la_immigration_groups_editions`; append-only, triggers refuse UPDATE,
  DELETE and TRUNCATE) and their four `_file_checks` ledgers. No live table, column, key, FK, the view
  `vw_la_asylum_support_totals` or `asylum_series_breaks` was touched.
- `migrate-legacy ... --commit` (after a preview and a `--simulate` that rolled back, both equal to the earlier
  read-only preview): edition 1 "as loaded" for 34 + 28 + 34 + 2 periods (21,953 + 84 + 2,553 + 7,104 = 31,694 rows),
  copied from the live rows; ledger rows for the files, outcome `unchanged` (34, 28, 34 and 2 ledger rows). Run-log
  row 324, 31,694 rows. Live untouched. The raw files stay in `data/raw/s6_asylum` (git-ignored) and are unchanged;
  verify gates 6, 7, 13, 16 and 22 find them by sha256.
- `load` preview: both page files are the held ones by (final URL, sha256), nothing parsed, "nothing to do". The
  Asy_D11 page lists year ending June 2026 as newest, Reg_02 the same, both already held; no WARNING (before 26
  November 2026). `load --recheck 2026-06-30`: every Asy_D11 period (96 table-periods) and Reg_02 2026-06-30
  unchanged, 0 rows changed. `load --file` of the held June Asy_D11 and Asy_D09 files with `--recheck 2026-06-30`, and
  of the held March Reg_02 file with `--recheck 2026-03-31`: unchanged. `load --file` of the March 2026 Asy_D11 copy
  with `--no-d09`: all 33 periods older than the held June release, HALT, exit 1, nothing written. So no
  `load --commit` was made. `refresh-latest` preview: nothing planned in any of the four tables. `status`: OK.
- After the migration the live tables recompute to the same row counts, survey hashes and `loaded_at` values, and to
  the same 196 hash lines as the before-state. No `zz%` table exists.
- Not run: W1, `refresh_map.py`, the export, `push.py`, `git push`. S6 is not on the map (registry `publish_map`
  false), so the map is unaffected.

## Each quarter (next: 26 November 2026, year ending September 2026)

The covers say "Next update: 26 November 2026"; the new period is 2026-09-30. From `ONS_Population_Estimates`:

1. `python scripts/s6_asylum_editions.py load` finds the newest Asy_D11, Asy_D09 and Reg_02 releases on their GOV.UK
   pages, downloads them to `data/raw/s6_asylum` (also in a preview; the database is not written), prints which
   release it saw as newest, reads each file's identity and reconciliations, and previews. Read the whole preview.
   After 26 November 2026, if nothing newer is listed, it prints a WARNING that the held file's next update has
   passed.
2. `load --commit` stores the new quarter (edition 1, live rows and ledger rows for the three Asy_D11 tables in one
   savepoint) and records unchanged held periods in the ledger. A restated back period is stored as the next
   edition; the live table changes only through `refresh-latest` (preview, then `--commit`, with
   `--accept-key-changes PERIOD` if the preview lists added or dropped keys), which sets `source_edition` on every
   row of the period and copies the edition's `loaded_at`. Then `status` and
   `python scripts/s6_asylum_editions_verify.py` (exit 0).
3. What a halt means: an older file (by its own cover) is skipped per period and halts if every period is older
   (`--allow-older-file` overrides and is logged); equal rank with different content needs `--accept-reissue PERIOD`;
   REJECTED (nothing stored for the period, no ledger row, exit 1) is an identity, header, value, reconciliation or
   geography failure, a new quarter moving beyond the thresholds, a revision beyond them without `--acknowledge`, or a
   partial file (never released). A blank, zero or text `People` cell halts; a Reg_02 cell other than an integer or `*`
   halts; a 0 to `*` flip needs `--acknowledge PERIOD`. A changed header, a new support or accommodation type, or an
   unexplained code is fixed deliberately, with evidence, before the load completes.
4. S6 is not a W1 input. If it ever is wired in, run `refresh-latest --commit` in the same session before W1, because
   refresh copies the edition's `loaded_at` and `refresh_map.py` would otherwise not see the revision.

## How to reverse

`python scripts/s6_asylum_editions.py restore-edition TABLE PERIOD N` (preview by default) stores edition N's rows as
the next edition for that table and period; `refresh-latest --commit` then applies it to the live table. To return a
period to the held state, restore edition 1 and refresh. Nothing is ever deleted from the editions tables.

## Notes and follow-ups

- The held `LOWER BOUND` wording ("True total is higher by between 1 and 4 per suppressed pathway") on four
  `all_pathways` rows is the old build's marker, kept exactly so held rows are unchanged. It is the held marker, not a
  publisher statement; the publisher's note 9 says only that `*` is fewer than 5 with secondary suppression.
- `percentage_of_population` in Reg_02 is the publisher's ratio (a fraction, despite the "(%)" header; 15 significant
  digits), stored rounded to 4 decimals by the column type. Changing the rounding is a follow-up; no held value
  changes here.
- The 12 earlier Reg_02 snapshots (March 2023 to December 2025, some with different layouts: March 2024 has 18
  columns) are not loaded. Each would be a new period, loadable later with `--release` after a header alias with
  evidence. Asy_D11 before 2018 has no local authority geography for Section 4 and is not loaded.
- Engine wording, not fixed here: `status` before the migration said "run sync-new" (the engine's text; S6 has no
  `sync-new`, and the same message appears in S22), and `pe.log_run` writes the transaction start as `completed_at`.
- The old scripts are retired in a later commit (Task 4 of the plan), with RETIRED guards.
