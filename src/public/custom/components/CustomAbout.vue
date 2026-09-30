<template>
  <div class="custom-about">
    <div class="custom-about-header">
      <span class="h2 custom-about-title">DIGIBAT: Bridging the gap between atomic discovery and industrial R&D</span>
    </div>

    <div class="custom-about-content">
      <span class="h3">What is DIGIBAT?</span>
      <p>The automated High-throughput facility for advanced energy research (DIGIBAT) at Imperial College London is the UK's first fully automated laboratory dedicated to battery and electrocatalysis research. Our mission is to accelerate the discovery cycle for sustainable energy materials by replacing manual bottlenecks with a unified "Digital Twin Lab" infrastructure.</p>
    </div>

    <div class="custom-about-cards">
      <a href="https://www.imperial.ac.uk/digibat" target="_blank" class="custom-about-card">Website</a>
      <a href="https://www.imperial.ac.uk/digibat/about-us/meet-the-team" target="_blank" class="custom-about-card">Team</a>
      <a href="https://www.imperial.ac.uk/digibat/capabilities" target="_blank" class="custom-about-card">Capabilities</a>
      <a href="https://www.linkedin.com/company/digibatatimperial" target="_blank" class="custom-about-card">LinkedIn</a>
      <a href="https://github.com/digibatatimperial/Digital-twin" target="_blank" class="custom-about-card">GitHub</a>
    </div>
    
    <div class="custom-about-callout">
      <span class="h3">For AI Researchers: The Benchmark Dataset</span>
      <p><b>July 2026</b> — we have released the <b>DIGIBAT Battery Discovery Benchmark</b>, containing over <b>250 fully characterized battery cells</b>, featuring:</p>
      <ul>
        <li><b>Controlled Variables</b>: Varied spacer thicknesses, separator types, and electrolyte volumes.</li>
        <li><b>Multi-Modal Data</b>: Integrated EIS, CV, SEM, XRD, and cycle-life metrics.</li>
        <li><b>Programmatic Access</b>: All data is available via the <i>datalab</i> API for direct integration with Python-based AI agents and Large Language Models.</li>
      </ul>

      <p>Read the Henry Royce Institute's press release <a href="https://www.royce.ac.uk/news/discovery-benchmark-released-open-battery-dataset-showcases-the-future-of-ai-enabled-materials-discovery" target="_blank">here</a> with accompanying video on the <a href="https://www.youtube.com/watch?v=nM_ktXMItOw" target="_blank">DIGIBAT YouTube channel</a>.</p>

      <p>You can download the dataset from Zenodo at <a href="https://doi.org/10.5281/zenodo.20532538">10.5281/zenodo.20532538</a>.</p>
    </div>
    
    <div class="custom-about-content">
      <span class="h3">Access & Collaboration</span>
      <p>DIGIBAT is funded by the EPSRC (<a href="https://gtr.ukri.org/projects?ref=EP%2FW036517%2F1">EP/W036517/1</a>) and is accessible to both academia and industry. We operate under strict data security protocols, ensuring that industrial IP is siloed and protected by robust confidentiality agreements.</p>
      <ul>
        <li><b>Contact us</b>: <a href="mailto:digibat@imperial.ac.uk">digibat@imperial.ac.uk</a></li>
        <li><b>Book equipment</b>: <a href="https://ppms.eu/imperial/" target="_blank">Imperial PPMS Booking System</a></li>
      </ul>

      <div align="center">
        <img src="/custom/logos/epsrc.png" alt="EPSRC logo" width="150" class="custom-about-funding-logo" style="margin-top: 1rem;" />
      </div>

    </div>
  </div>
</template>

<script>
export default {
  name: "CustomAbout",
};
</script>

<style scoped>
.custom-about {
  font-family: "Imperial Sans Display", sans-serif;
  font-weight: 500;
}

.custom-about a {
  font-family: "Imperial Sans Display", sans-serif;
  color: #0000cd;
}

.custom-about .h3,
.custom-about .h5 {
  font-family: "Imperial Sans Display", sans-serif;
  font-weight: 700;
  font-size: 1.2rem;
  color: #0000cd;
}

.custom-about-header {
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1.5rem;
}

.custom-about-logo {
  max-height: 80px;
  width: auto;
}

.custom-about-title {
  font-family: "Imperial Sans Display", sans-serif;
  font-weight: 700;
  color: #0000cd;
  margin: 0;
}

.custom-about-cards {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 0.5rem;
  margin-bottom: 2.5rem;
  margin-top: 1.5rem;
}

.custom-about-card {
  flex: 0 1 auto;
  padding: 0.4rem 0.8rem;
  border: 1px solid #e0e0e0;
  border-radius: 6px;
  text-decoration: none;
  color: #0000cd;
  font-weight: 700;
  font-size: 1rem;
  background: #f9f9f9;
  transition: box-shadow 0.2s ease, border-color 0.2s ease;
}

.custom-about-card:hover {
  border-color: #0000cd;
  box-shadow: 0 2px 8px rgba(0, 0, 205, 0.15);
}

.custom-about-callout {
  border: 1px solid #ccc;
  border-radius: 8px;
  margin: 2rem 0;
  background: #faf8f5;
  overflow: hidden;
}

.custom-about-callout .h3 {
  display: block;
  margin: 0 0 1rem;
  padding: 0.75rem 1.25rem;
  background: #f0ece6;
  border-bottom: 1px solid #ccc;
}

.custom-about-callout p,
.custom-about-callout ul {
  padding-left: 1.25rem;
  padding-right: 1.25rem;
}

.custom-about-callout ul {
  list-style-position: inside;
  padding-left: 1.25rem;
}
</style>
