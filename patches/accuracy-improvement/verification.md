# Accuracy improvement verification

## Baseline

Command:

```powershell
Get-FileHash .\patches\accuracy-improvement\original\train_npedi_captcha_cnn.py -Algorithm SHA256
```

Output (`exit 0`):

```text
331FA48CA103D931A5F7F981FA4A4D3E974B0AAEB7A268021E1A8200DB075D48
```

Baseline behavior: `ModelCheckpoint` and `EarlyStopping` monitored
`val_categorical_accuracy`; model initialization had no explicit TensorFlow seed.

## Modified checks

Command:

```powershell
$py = '.\.captcha-cnn-venv\Scripts\python.exe'
& $py -m py_compile .\scripts\train_npedi_captcha_cnn.py .\scripts\cross_validate_npedi_captcha_cnn.py
git diff --check -- scripts\train_npedi_captcha_cnn.py
```

Literal output/status:

```text
py_compile_exit=0
diff_check_exit=0
```

Modified behavior: initialization uses `random_seed`; checkpoints and early stopping
monitor `val_whole_captcha_accuracy`.

## 5-fold baseline

Command:

```powershell
& $py .\scripts\cross_validate_npedi_captcha_cnn.py --folds 5 --output .\captcha-model\cross-validation
```

Input: 1188 labeled training images; fixed seed `20260805`; 200 test images excluded.

Literal result (`exit 0`):

```text
mean_whole_accuracy=0.9495231003793922
std_whole_accuracy=0.021713295100747296
fold_accuracy=[0.9117647058823529, 0.9621848739495799, 0.9411764705882353, 0.9578059071729957, 0.9746835443037974]
best_epoch=[94, 97, 71, 86, 67]
```

## Independent test observation

Input: all 200 images under `captcha-data/test/labeled`.

Literal result (`exit 0`):

```text
current_single_model=189/200=0.945
five_fold_probability_ensemble=193/200=0.965
ensemble_character_accuracy=0.99125
```

The existing final weight was not overwritten:

```text
captcha-model/npedi.weights.h5
SHA256=692CC4DD26B203CD6E98555E0FE57C12C1F2820588BFDCA2F7DBA5BDC46A8FA6
modified=2026-08-06 22:26:17
```

## Rollback and replay

Commands:

```powershell
& .\patches\accuracy-improvement\rollback.ps1
git apply --check .\patches\accuracy-improvement\changes.patch
git apply .\patches\accuracy-improvement\changes.patch
```

Literal output/status:

```text
rollback_success=True
restored_hash=331FA48CA103D931A5F7F981FA4A4D3E974B0AAEB7A268021E1A8200DB075D48
cross_validation_exists=False
patch_check_exit=0
patch_apply_exit=0
cross_validation_restored=True
```
