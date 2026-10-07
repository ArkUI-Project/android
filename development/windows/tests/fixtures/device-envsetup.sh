breakfast() {
  export ANDROID_PRODUCT_OUT=/wrong/inherited/output
}

get_abs_build_var() {
  printf '%s/out/target/product/emu64x\n' "$ARKUI_SOURCE"
}
