<!-- This file was edited with the assistance of an AI model and requires human review from the contributor. -->
<template>
  <slot name="login" />
  <hr class="my-4" />
  <div class="custom-login-about">
    <CustomAbout />
  </div>
</template>

<script>
import CustomAbout from "./CustomAbout.vue";

export default {
  name: "CustomLoginInfo",
  components: { CustomAbout },
};
</script>

<style>
/* Widen the login box so the about content is readable */
:root {
  --login-max-width: 50rem;
}
</style>

<style scoped>
.custom-login-about {
  text-align: left;
}
</style>
